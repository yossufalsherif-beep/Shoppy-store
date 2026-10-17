import os, sqlite3, secrets, hashlib, hmac, json, re, time, uuid, urllib.request
from decimal import Decimal, InvalidOperation
from werkzeug.utils import secure_filename
from pathlib import Path
from functools import wraps
from flask import Flask, request, redirect, session, render_template, abort, flash, jsonify
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from shopy_notify import init_notification_store, queue_new_order, configured_recipients
from safka_bridge import init_safka_schema, quote_safka_shipping, shipping_pricing_document_id, queue_safka_order, supplier_stock_ready, CatalogValidationError
from safka_dry_run import prepare_dry_run
from safka_live_orders import (canonical_order, load_contract, build_payload,
    submit_payload, claim_one_test_order, record_outcome, OrderContractError)
from safka_cleanup import summary as safka_cleanup_summary, cleanup as safka_cleanup, CleanupNotReady, CONFIRM_TEXT
ROOT=Path(__file__).parent
load_dotenv(ROOT / '.env')
if os.environ.get('VERCEL') and (not os.getenv('SECRET_KEY') or not os.getenv('DATABASE_URL')):
 raise RuntimeError('SECRET_KEY and DATABASE_URL are required for Vercel staging')
# Optional preview password protection; public read-only previews do not require it.
if os.getenv('SHOPY_STAGING_BASIC_AUTH_ENABLED') == '1' and len(os.getenv('SHOPY_STAGING_PASSWORD','')) < 12:
 raise RuntimeError('SHOPY_STAGING_PASSWORD (12+ chars) required when staging basic auth is enabled')
app=Flask(__name__); app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=50*1024*1024)
app.config['SESSION_COOKIE_SECURE'] = bool(os.environ.get('VERCEL'))
# HTTPS cookies in Vercel; local HTTP remains supported.
STAGING_MODE = bool(os.environ.get('VERCEL') or os.environ.get('SHOPY_STAGING_MODE')=='1')
STAGING_BASIC_AUTH_ENABLED = STAGING_MODE and os.getenv('SHOPY_STAGING_BASIC_AUTH_ENABLED','0') == '1'
DB=os.environ.get('DATABASE_PATH',str(ROOT/'shopy.db'))
UPLOADS=ROOT/'static'/'uploads'
# File uploads stay local in V3; configure a persistent volume before public deployment.
def db():
 if os.environ.get('DATABASE_URL'):
  from pg_backend import PGConnection
  return PGConnection()
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c
def init():
 with db() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,email TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'customer');
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,title TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,description TEXT NOT NULL DEFAULT '',price INTEGER NOT NULL CHECK(price>=0),image TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS shipping(id INTEGER PRIMARY KEY,governorate TEXT UNIQUE NOT NULL,fee INTEGER NOT NULL CHECK(fee>=0));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,reference TEXT UNIQUE NOT NULL,user_id INTEGER REFERENCES users(id),customer TEXT NOT NULL,phone TEXT NOT NULL,governorate TEXT NOT NULL,address TEXT NOT NULL,shipping_fee INTEGER NOT NULL,total INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'new',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),product_id INTEGER REFERENCES products(id),title TEXT NOT NULL,unit_price INTEGER NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0));''')
  c.execute('''CREATE TABLE IF NOT EXISTS product_media (id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,kind TEXT NOT NULL CHECK(kind IN ('image','video')),path TEXT NOT NULL,position INTEGER NOT NULL DEFAULT 0)''')
  init_notification_store(c)
  # Forward-only, additive SQLite migration, preserving all V2 rows.
  pc={r['name'] for r in c.execute('PRAGMA table_info(products)')}
  if 'options_json' not in pc:c.execute("ALTER TABLE products ADD COLUMN options_json TEXT NOT NULL DEFAULT '{}'")
  ic={r['name'] for r in c.execute('PRAGMA table_info(order_items)')}
  if 'variant_json' not in ic:c.execute("ALTER TABLE order_items ADD COLUMN variant_json TEXT NOT NULL DEFAULT '{}'")
  oc={r['name'] for r in c.execute('PRAGMA table_info(orders)')}
  if 'checkout_key' not in oc:c.execute('ALTER TABLE orders ADD COLUMN checkout_key TEXT')
  c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_checkout_key ON orders(checkout_key)')
  # V4: additive migrations only. Old orders, users and media are preserved.
  product_columns={r['name'] for r in c.execute('PRAGMA table_info(products)')}
  for column,definition in [('category',"TEXT NOT NULL DEFAULT ''"),('supplier_cost','INTEGER')]:
   if column not in product_columns:c.execute(f'ALTER TABLE products ADD COLUMN {column} {definition}')
  shipping_columns={r['name'] for r in c.execute('PRAGMA table_info(shipping)')}
  if 'supplier_cost' not in shipping_columns:c.execute('ALTER TABLE shipping ADD COLUMN supplier_cost INTEGER')
  order_columns={r['name'] for r in c.execute('PRAGMA table_info(orders)')}
  for column,definition in [('utm_source',"TEXT NOT NULL DEFAULT ''"),('utm_campaign',"TEXT NOT NULL DEFAULT ''"),('shipping_supplier_cost','INTEGER')]:
   if column not in order_columns:c.execute(f'ALTER TABLE orders ADD COLUMN {column} {definition}')
  item_columns={r['name'] for r in c.execute('PRAGMA table_info(order_items)')}
  if 'unit_cost' not in item_columns:c.execute('ALTER TABLE order_items ADD COLUMN unit_cost INTEGER')
  c.executescript('''CREATE TABLE IF NOT EXISTS store_settings (key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS shipping_zones (id INTEGER PRIMARY KEY,governorate TEXT NOT NULL,city TEXT NOT NULL DEFAULT '',area TEXT NOT NULL DEFAULT '',fee INTEGER NOT NULL CHECK(fee>=0),supplier_cost INTEGER, UNIQUE(governorate,city,area));
CREATE INDEX IF NOT EXISTS idx_products_category ON products(active,category);
CREATE INDEX IF NOT EXISTS idx_orders_utm ON orders(utm_source,utm_campaign);''')
  init_safka_schema(c)
  # Never auto-seed demonstration catalog or arbitrary shipping prices.
  # User-owned records remain until an explicit backup + cleanup operation.
def csrf():return session.setdefault('csrf',secrets.token_urlsafe(24))
@app.template_filter('options_from_json')
def options_from_json(value):
 try:return json.loads(value or '{}')
 except (ValueError,TypeError):return {}

def safka_only_mode():
 return store_settings().get('catalog_mode')=='safka_only'

@app.context_processor
def context():
 return {'csrf':csrf(),'user':current_user(),'cart_count':sum(q for q in session.get('cart',{}).values() if isinstance(q,int)),'money':lambda x:f'{x/100:,.2f}', 'contact':store_settings(),'staging_mode':STAGING_MODE}
def current_user():
 uid=session.get('uid')
 if not uid:return None
 with db() as c:return c.execute('SELECT id,email,role FROM users WHERE id=?',(uid,)).fetchone()
@app.before_request
def staging_gate():
 if STAGING_MODE:
  private_password=os.environ.get('SHOPY_STAGING_PASSWORD','')
  if STAGING_BASIC_AUTH_ENABLED and private_password and request.path!='/health':
   auth=request.authorization
   if not auth or not hmac.compare_digest(auth.password or '',private_password):
    return app.response_class('Staging access only',401,{'WWW-Authenticate':'Basic realm="Shopy Staging"'})
  # Block customer checkout and account registration during integration testing.
  if request.path.startswith('/register'):
   abort(503,'Staging mode: public registration disabled')
  if request.path.startswith('/checkout'):
   # Only the authenticated owner may generate a staging order for a live
   # one-order provider test. Other visitors never get checkout access.
   owner = current_user() if os.getenv('SHOPY_STAGING_OWNER_CHECKOUT') == '1' else None
   if not owner or owner['role'] != 'admin':
    abort(503,'Staging mode: customer checkout disabled')

@app.after_request
def noindex_staging(resp):
 if STAGING_MODE:
  resp.headers['X-Robots-Tag']='noindex, nofollow'
  resp.headers['Cache-Control']='no-store' if request.path.startswith(('/admin','/account','/login')) else 'private, no-cache'
 return resp

@app.before_request
def protect():
 if request.method in ('POST','PUT','PATCH','DELETE'):
  if not hmac.compare_digest(request.form.get('csrf',''),session.get('csrf','_')):abort(400,'Invalid CSRF token')
def admin_only(fn):
 @wraps(fn)
 def wrapper(*a,**k):
  u=current_user()
  if not u:return redirect('/login')
  if u['role']!='admin':abort(403)
  return fn(*a,**k)
 return wrapper
def capture_campaign():
 # Only simple, bounded attribution labels. No user identity, cookies or IP analytics.
 for name in ('utm_source','utm_campaign'):
  value=request.args.get(name)
  if value is not None:
   session[name]=re.sub(r'[^a-zA-Z0-9_\-\u0600-\u06FF ]','',value)[:80]

def store_settings():
 with db() as conn:return {r['key']:r['value'] for r in conn.execute('SELECT key,value FROM store_settings')}

def shipping_quote(conn, governorate,city='',area=''):
 """Region-specific override beats city override beats governorate base rate."""
 governorate=governorate.strip(); city=city.strip();area=area.strip()
 if not governorate:return None
 exact=conn.execute('SELECT fee,supplier_cost FROM shipping_zones WHERE governorate=? AND city=? AND area=?', (governorate,city,area)).fetchone() if city and area else None
 if exact:return exact['fee'],exact['supplier_cost']
 broader=conn.execute("SELECT fee,supplier_cost FROM shipping_zones WHERE governorate=? AND city=? AND area=''",(governorate,city)).fetchone() if city else None
 if broader:return broader['fee'],broader['supplier_cost']
 base=conn.execute('SELECT fee,supplier_cost FROM shipping WHERE governorate=?',(governorate,)).fetchone()
 if base:return base['fee'],base['supplier_cost']
 return None

@app.route('/')
def home():
 capture_campaign()
 term=request.args.get('q','').strip()[:80]; category=request.args.get('category','').strip()[:200]
 with db() as c:
  filters=['active=1'];args=[]
  if term:filters.append('(title LIKE ? OR description LIKE ?)');args.extend(['%'+term+'%','%'+term+'%'])
  if category:filters.append('category=?');args.append(category)
  items=c.execute('SELECT * FROM products WHERE '+' AND '.join(filters)+' ORDER BY id DESC',args).fetchall()
  categories=[r['category'] for r in c.execute("SELECT DISTINCT category FROM products WHERE active=1 AND category!='' ORDER BY category")]
 return render_template('home.html',items=items,term=term,category=category,categories=categories)

@app.route('/products/<slug>')
def product(slug):
 capture_campaign()
 with db() as c:
  p=c.execute('SELECT * FROM products WHERE slug=? AND active=1',(slug,)).fetchone()
  if not p:abort(404)
  others=c.execute("SELECT * FROM products WHERE active=1 AND id!=? ORDER BY CASE WHEN category=? AND category!='' THEN 0 ELSE 1 END,id DESC LIMIT 4",(p['category'],p['id'])).fetchall()
 with db() as c:media=c.execute('SELECT * FROM product_media WHERE product_id=? ORDER BY position,id',(p['id'],)).fetchall()
 return render_template('product.html',p=p,others=others,media=media,options=json.loads(p['options_json'] or '{}'),stock_ready=supplier_stock_ready(p))
def product_choices(p):
 try:
  data=json.loads(p['options_json'] or '{}')
  if not isinstance(data,dict):return {}
  return {k:v for k,v in data.items() if isinstance(k,str) and isinstance(v,list)}
 except (ValueError,TypeError):return {}

def encoded_cart_key(pid, choices):
 if not choices:return str(pid)
 # Deterministic and backwards-compatible with V2 plain product-id keys.
 return str(pid)+':'+json.dumps(choices,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def decoded_cart_key(key):
 pid,_,raw=key.partition(':')
 if not pid.isdigit():return None,None
 try:choices=json.loads(raw) if raw else {}
 except ValueError:return None,None
 if not isinstance(choices,dict):return None,None
 return int(pid),choices

@app.post('/cart/add/<int:pid>')
def add_cart(pid):
 with db() as c:p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
 if not p:abort(404)
 options=product_choices(p);chosen={}
 for key, values in options.items():
  val=request.form.get('option_'+key,'')
  if val not in values:flash('اختار '+key+' قبل الإضافة للسلة');return redirect('/products/'+p['slug'])
  chosen[key]=val
 cart=session.get('cart',{});key=encoded_cart_key(pid,chosen)
 new_quantity=min(99,int(cart.get(key,0))+1)
 # A supplier stock count applies to ALL size/color choices of this product
 # until Safka confirms independently tracked SKU-level inventory.
 combined=sum(q for k,q in cart.items() if decoded_cart_key(str(k))[0]==pid and isinstance(q,int) and k!=key)+new_quantity
 if p['source']=='safka' and not supplier_stock_ready(p,combined):
  flash('الكمية المطلوبة مش متوفرة حسب آخر مزامنة مع صفقة');return redirect('/products/'+p['slug'])
 cart[key]=new_quantity;session['cart']=cart
 flash('تمت إضافة المنتج للسلة')
 return redirect('/cart')

def cart_data():
 rows=[]
 with db() as c:
  for key,q in session.get('cart',{}).items():
   pid,choices=decoded_cart_key(str(key))
   if pid is None or not isinstance(q,int) or q<1 or q>99:continue
   p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
   if not p:continue
   valid=product_choices(p)
   if set(choices)!=set(valid):continue
   if any(choices.get(k) not in vals for k,vals in valid.items()):continue
   if p['source']=='safka':
    # Never sell an item with unknown/insufficient supplier inventory.
    if not supplier_stock_ready(p,q):continue
   rows.append((p,q,choices,key))
 return rows

@app.route('/cart')
def cart():return render_template('cart.html',rows=cart_data())

@app.post('/cart/remove')
def remove():
 key=request.form.get('key','');cart=session.get('cart',{});cart.pop(key,None);session['cart']=cart
 return redirect('/cart')

@app.get('/api/shipping-quote')
def quote_shipping():
 rows=cart_data()
 if rows and any(p['source']=='safka' for p,_,_,_ in rows) and any(p['source']!='safka' for p,_,_,_ in rows):
  return jsonify(ok=False,message='لا يمكن شحن منتجات موردين مختلفين في طلب واحد حاليًا'),400
 with db() as conn:
  provider_cart=any(p['source']=='safka' for p,_,_,_ in rows)
  fn=quote_safka_shipping if provider_cart else shipping_quote
  quoted=fn(conn,request.args.get('governorate',''),request.args.get('city',''),request.args.get('area',''))
 if quoted is None:return jsonify(ok=False,message='الشحن غير متاح للعنوان المحدد'),404
 return jsonify(ok=True,shipping_piastres=quoted[0])

@app.route('/checkout',methods=['GET','POST'])
def checkout():
 rows=cart_data()
 if not rows:return redirect('/cart')
 if not session.get('checkout_key'):session['checkout_key']=secrets.token_urlsafe(24)
 supplier_cart=any(p['source']=='safka' for p,_,_,_ in rows)
 mixed_cart=supplier_cart and any(p['source']!='safka' for p,_,_,_ in rows)
 with db() as c:
  rates=c.execute('SELECT DISTINCT governorate FROM safka_shipping_rates ORDER BY governorate').fetchall() if supplier_cart else c.execute('SELECT * FROM shipping ORDER BY governorate').fetchall()
 if request.method=='POST':
  if len(rows)!=len(session.get('cart',{})):
   flash('السلة فيها منتج لم يعد متاحًا أو بياناته قديمة. راجع السلة قبل تأكيد الطلب.')
   return redirect('/cart')
  name=request.form.get('name','').strip()[:120];phone=request.form.get('phone','').strip();gov=request.form.get('governorate','').strip()
  address=request.form.get('address','').strip()[:500];city=request.form.get('city','').strip()[:80];area=request.form.get('area','').strip()[:80]
  if len(name)<2 or not re.fullmatch(r'01[0125]\d{8}',phone) or len(address)<8 or len(city)<2 or len(area)<2:
   flash('راجع الاسم ورقم الموبايل والمدينة والمنطقة والعنوان التفصيلي')
   return render_template('checkout.html',rows=rows,rates=rates)
  key=session['checkout_key']
  try:
   with db() as c:
    c.execute('BEGIN IMMEDIATE')
    existing=c.execute('SELECT reference FROM orders WHERE checkout_key=?',(key,)).fetchone()
    if existing:return redirect('/order/success/'+existing['reference'])
    if mixed_cart:raise ValueError('قسم الطلب: لا يمكن شحن منتجات صفقة ومنتجاتك اليدوية معًا حاليًا')
    rate=(quote_safka_shipping if supplier_cart else shipping_quote)(c,gov,city,area)
    if rate is None:raise ValueError('لا يوجد سعر شحن مؤكد من المورد لهذه المنطقة')
    fresh=[]
    quantities_by_product={}
    for p,q,choices,_ in rows:
     quantities_by_product[p['id']]=quantities_by_product.get(p['id'],0)+q
    for p,q,choices,_ in rows:
     p2=c.execute('SELECT * FROM products WHERE id=? AND active=1',(p['id'],)).fetchone()
     if not p2 or any(choices.get(k) not in vals for k,vals in product_choices(p2).items()) or set(choices)!=set(product_choices(p2)):
      raise ValueError('بعض المنتجات أو الخيارات لم تعد متاحة، راجع السلة')
     if p2['source']=='safka' and not supplier_stock_ready(p2,quantities_by_product[p2['id']]):
      raise ValueError('مخزون صفقة الحالي غير مؤكد أو لا يكفي لهذا المنتج؛ راجع السلة')
     fresh.append((p2,q,choices))
    total=sum(p['price']*q for p,q,_ in fresh)+rate[0];ref='SH-'+secrets.token_hex(6).upper()
    full_address=' | '.join((city,area,address))
    pricing_doc=(shipping_pricing_document_id(c,gov,city,area,rate[0],rate[1]) if supplier_cart else None)
    cur=c.execute('''INSERT INTO orders(reference,user_id,customer,phone,governorate,address,shipping_fee,total,
       checkout_key,utm_source,utm_campaign,shipping_supplier_cost,safka_shipping_pricing_document_id)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(ref,session.get('uid'),name,phone,gov,full_address,rate[0],total,key,
       session.get('utm_source',''),session.get('utm_campaign',''),rate[1],pricing_doc))
    for p,q,choices in fresh:
     c.execute('INSERT INTO order_items(order_id,product_id,title,unit_price,quantity,variant_json,unit_cost) VALUES(?,?,?,?,?,?,?)',(cur.lastrowid,p['id'],p['title'],p['price'],q,json.dumps(choices,ensure_ascii=False),p['supplier_cost']))
    # Stage Safka order in the same transaction; submission requires verified API schema.
    queue_safka_order(c,cur.lastrowid)
    # Durable events: same transaction as saved order, one event per owner recipient.
    # Never depend on WhatsApp success for completing checkout.
    # A broken configuration must not lose orders; refuse real sending by default.
    try:
     recipients=configured_recipients()
    except ValueError:
     recipients=[]
    queue_new_order(c,{
     'order_ref':ref,'customer_name':name,'customer_phone':phone,
     'governorate':gov,'city':city,'area':area,
     'products_total_piasters':total-rate[0],
     'shipping_piasters':rate[0],'total_piasters':total,
     'items':[{'name':p['title'],'qty':q,'options':choices} for p,q,choices in fresh],
     'payment_method':'الدفع عند الاستلام',
    },recipients=recipients,enabled=os.getenv('SHOPY_WA_ENABLED')=='1')
    c.commit()
  except ValueError as exc:
   flash(str(exc));return render_template('checkout.html',rows=rows,rates=rates)
  session['cart']={};session.pop('checkout_key',None)
  session.setdefault('my_order_refs',[]).append(ref)
  return redirect('/order/success/'+ref)
 return render_template('checkout.html',rows=rows,rates=rates)

@app.route('/order/success/<ref>')
def order_success(ref):
 if ref not in session.get('my_order_refs',[]):abort(404)
 with db() as c:order=c.execute('SELECT reference,total,shipping_fee FROM orders WHERE reference=?',(ref,)).fetchone()
 if not order:abort(404)
 return render_template('success.html',ref=ref,order=order)

@app.route('/register',methods=['GET','POST'])
def register():
 if request.method=='POST':
  email=request.form.get('email','').strip().lower();pw=request.form.get('password','')
  if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(pw)<10:flash('بريد صحيح وكلمة مرور 10 أحرف على الأقل')
  else:
   try:
    with db() as c:
     cur=c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',(email,generate_password_hash(pw),'customer'));session.clear();session['uid']=cur.lastrowid
    return redirect('/account')
   except Exception as exc:
    if isinstance(exc, sqlite3.IntegrityError) or (exc.__class__.__name__ == 'UniqueViolation' and exc.__class__.__module__.startswith('psycopg')):flash('البريد مستخدم بالفعل')
    else:raise
 return render_template('auth.html',mode='register')
@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  with db() as c:u=c.execute('SELECT * FROM users WHERE email=?',(request.form.get('email','').strip().lower(),)).fetchone()
  if u and check_password_hash(u['password'],request.form.get('password','')):
   session.clear();session['uid']=u['id'];return redirect('/admin' if u['role']=='admin' else '/account')
  flash('بيانات الدخول غير صحيحة')
 return render_template('auth.html',mode='login')
@app.post('/logout')
def logout():session.clear();return redirect('/')
@app.route('/account')
def account():
 u=current_user()
 if not u:return redirect('/login')
 with db() as c:orders=c.execute('SELECT * FROM orders WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall()
 return render_template('account.html',orders=orders)
@app.get('/admin/safka/order/<int:oid>/preview')
@admin_only
def preview_safka_order(oid):
 try:
  with db() as conn:
   preview=prepare_dry_run(conn,oid)
 except CatalogValidationError as exc:
  flash('تعذر تجهيز معاينة صفقة: '+str(exc))
  return redirect('/admin#orders')
 with db() as conn:
  outbox=conn.execute('SELECT status,supplier_order_id,attempt_count,submitted_commission_piastres FROM safka_order_outbox WHERE order_id=?',(oid,)).fetchone()
 test_enabled=(STAGING_MODE and os.getenv('SHOPY_SAFKA_TEST_SEND_ENABLED')=='1'
                and os.getenv('SHOPY_SAFKA_ORDER_PUSH_ACTIVE','0')=='0')
 return render_template('safka_preview.html',preview=preview,outbox=outbox,order_id=oid,
                        test_enabled=test_enabled)

@app.post('/admin/safka/order/<int:oid>/submit-test')
@admin_only
def submit_safka_test_order(oid):
 # One explicit owner-confirmed live provider POST; never automated in V4.3-test.
 # Disabled unless operator enables staging + a verified Safka order contract.
 if not STAGING_MODE or os.getenv('SHOPY_SAFKA_TEST_SEND_ENABLED')!='1':
  abort(403,'Real provider test is not enabled')
 if os.getenv('SHOPY_SAFKA_ORDER_PUSH_ACTIVE','0')!='0':
  abort(403,'Automatic order submission must remain disabled')
 expected=request.form.get('confirm_reference','').strip()
 try:
  if os.getenv('SHOPY_SAFKA_ORDER_SCHEMA_VERIFIED')!='1':
   raise OrderContractError('Create Order JSON contract has not been verified')
  contract=load_contract()
  api_key=os.getenv('SHOPY_SAFKA_API_KEY','')
  if len(api_key.strip()) < 12:
   raise OrderContractError('Add your private Safka API key on the server before sending')
  # The official required commission field is omitted from the docs' example.
  # The owner explicitly enters and verifies it for this ONE test; no speculative auto-pricing.
  commission_text=request.form.get('verified_commission_egp','').strip()
  if not re.fullmatch(r'\d{1,9}(?:\.\d{1,2})?', commission_text):
   raise OrderContractError('Enter the explicitly confirmed Safka commission as a non-negative EGP amount')
  commission=Decimal(commission_text)
  property_id=request.form.get('verified_property_id','').strip()
  if not re.fullmatch(r'[a-fA-F0-9]{24}',property_id):
   raise OrderContractError('Confirm the exact 24-character supplier property ID')
  # Validate all inputs before claiming a durable one-shot supplier order.
  with db() as conn:
   preview=prepare_dry_run(conn,oid)
   if preview['order_reference'] != expected:
    raise OrderContractError('Type the exact order reference to authorize the live POST')
   if len(preview['items'])!=1:
    raise OrderContractError('The first verified provider order test must contain exactly one line item')
   product_id=preview['items'][0]['supplier_product_id']
   if not re.fullmatch(r'[a-fA-F0-9]{24}',str(product_id)):
    raise OrderContractError('Supplier product ID must be verified against the Safka product record')
   canonical=canonical_order(preview,commission_egp=float(commission),property_ids={0:property_id})
   build_payload(canonical,contract)
  with db() as conn:
   claim_one_test_order(conn,oid,expected,int(commission*100))
  # There is NO second attempt if network fails or provider response is unclear.
  outcome=submit_payload(canonical,contract,api_key)
  with db() as conn:
   record_outcome(conn,oid,outcome)
  if outcome.state=='accepted':
   flash('صفقة قبلت الطلب برقم '+str(outcome.provider_order_id)+'؛ تأكد من ظهوره في حسابك قبل أي إرسال آخر')
  elif outcome.state=='rejected':
   flash('صفقة رفضت الطلب. لا تعيد إرساله قبل مراجعة البيانات والحالة في حساب صفقة')
  else:
   flash('حالة الإرسال غير مؤكدة. تحقق من حساب صفقة يدويًا؛ منعنا الإعادة لتفادي طلب مكرر')
 except (OrderContractError,CatalogValidationError,ValueError,OSError) as exc:
  flash('لم يتم بدء إرسال جديد: '+str(exc))
 return redirect('/admin/safka/order/'+str(oid)+'/preview')

@app.post('/admin/safka/replace-manual-catalog')
@admin_only
def replace_manual_catalog():
 text=request.form.get('confirmation','').strip()
 try:
  if os.environ.get('DATABASE_URL'):
   raise CleanupNotReady('الحذف السحابي مقفول لحين نسخة PostgreSQL احتياطية مؤكدة يدويًا.')
  with db() as conn:
   result=safka_cleanup(conn,DB,ROOT/'backups',text)
 except (CleanupNotReady, OSError, sqlite3.Error) as exc:
  flash('لم يتم حذف أي بيانات: '+str(exc))
  return redirect('/admin#integrations')
 flash('تم الاستبدال بأمان، وحفظ نسخة احتياطية خاصة من البيانات القديمة. المنتجات: '+str(result['removed_products'])+', تسعيرات الشحن: '+str(result['removed_shipping']+result['removed_zones']))
 return redirect('/admin#integrations')

@app.route('/admin')
@admin_only
def admin():
 with db() as c:orders=c.execute('SELECT * FROM orders ORDER BY id DESC LIMIT 100').fetchall();products=c.execute('SELECT * FROM products ORDER BY id DESC').fetchall();rates=c.execute('SELECT * FROM shipping ORDER BY governorate').fetchall()

 with db() as c:
  notify_status={o['reference']:c.execute('SELECT status,recipient FROM notification_outbox WHERE order_ref=? ORDER BY id',(o['reference'],)).fetchall() for o in orders}
  details={o['id']:c.execute('SELECT title,unit_price,quantity,variant_json FROM order_items WHERE order_id=?',(o['id'],)).fetchall() for o in orders}
  media_counts={p['id']:c.execute('SELECT COUNT(*) FROM product_media WHERE product_id=?',(p['id'],)).fetchone()[0] for p in products}
  product_media={p['id']:c.execute('SELECT * FROM product_media WHERE product_id=? ORDER BY position,id',(p['id'],)).fetchall() for p in products}
 with db() as c:
  zones=c.execute('SELECT * FROM shipping_zones ORDER BY governorate,city,area').fetchall()
  funnels=c.execute('''SELECT COALESCE(NULLIF(utm_source,''),'مباشر / غير معروف') AS source,COALESCE(NULLIF(utm_campaign,''),'بدون حملة') AS campaign,
COUNT(*) AS count,SUM(total) AS order_value FROM orders GROUP BY source,campaign ORDER BY count DESC LIMIT 12''').fetchall()
  states=c.execute('SELECT status,COUNT(*) AS count FROM orders GROUP BY status ORDER BY count DESC').fetchall()
  cost_rows=c.execute('''SELECT o.id AS order_id,o.total,o.shipping_supplier_cost,
SUM(CASE WHEN oi.unit_cost IS NULL THEN 1 ELSE 0 END) AS missing_cost,
SUM(CASE WHEN oi.unit_cost IS NULL THEN 0 ELSE oi.unit_cost*oi.quantity END) AS item_cost
FROM orders o LEFT JOIN order_items oi ON oi.order_id=o.id GROUP BY o.id''').fetchall()
  priced=[r for r in cost_rows if r['shipping_supplier_cost'] is not None and r['missing_cost']==0]
  estimated_margin=sum(r['total']-r['item_cost']-r['shipping_supplier_cost'] for r in priced)
  settings=store_settings()
  safka_states={row['order_id']:row for row in c.execute('SELECT * FROM safka_order_outbox')}
  safka_sync=c.execute('SELECT kind,updated,hidden,created_at,source_label FROM safka_sync_runs ORDER BY id DESC LIMIT 6').fetchall()
  safka_count=c.execute("SELECT COUNT(*) FROM products WHERE source='safka'").fetchone()[0]
  cleanup_state=safka_cleanup_summary(c)
 return render_template('admin.html',orders=orders,products=products,rates=rates,details=details,media_counts=media_counts,product_media=product_media,notify_status=notify_status,zones=zones,funnels=funnels,states=states,estimated_margin=estimated_margin,margin_order_count=len(priced),settings=settings,wa_configured=os.environ.get('SHOPY_WA_ENABLED')=='1',safka_states=safka_states,safka_sync=safka_sync,safka_count=safka_count,cleanup_state=cleanup_state,safka_only=safka_only_mode(),cleanup_text=CONFIRM_TEXT,api_read_enabled=os.getenv('SHOPY_SAFKA_READ_ENABLED')=='1',api_key_present=bool(os.getenv('SHOPY_SAFKA_API_KEY')))

def parse_money(value):
 try:
  amount=Decimal(str(value))
  if not amount.is_finite() or amount<0 or amount.as_tuple().exponent < -2:raise ValueError()
  cents=amount*100
  if cents>100000000000:raise ValueError()
  return int(cents)
 except (InvalidOperation,ValueError,TypeError):abort(400,'Invalid price')

def store_uploads(files):
 saved=[]
 for file in files:
  if not file or not file.filename:continue
  suffix=Path(secure_filename(file.filename)).suffix.lower()
  allowed={'.jpg':('image','image/jpeg'),'.jpeg':('image','image/jpeg'),'.png':('image','image/png'),'.webp':('image','image/webp'),'.mp4':('video','video/mp4'),'.webm':('video','video/webm')}
  if suffix not in allowed:abort(400,'Unsupported media format')
  kind,mime=allowed[suffix]
  # Validate signature, not just user-provided extension or content type.
  head=file.stream.read(16);file.stream.seek(0)
  valid=(head.startswith(b'\xff\xd8\xff') if suffix in ('.jpg','.jpeg') else head.startswith(b'\x89PNG\r\n\x1a\n') if suffix=='.png' else head.startswith(b'RIFF') and head[8:12]==b'WEBP' if suffix=='.webp' else b'ftyp' in head[4:12] if suffix=='.mp4' else head.startswith(b'\x1a\x45\xdf\xa3'))
  if not valid:abort(400,'Invalid media file')
  if os.environ.get('VERCEL') or os.environ.get('DATABASE_URL'):
   abort(503,'رفع الوسائط المحلي غير متاح على الاستضافة؛ استخدم روابط HTTPS للوسائط أو جهّز تخزينًا دائمًا.')
  UPLOADS.mkdir(parents=True,exist_ok=True)
  name=uuid.uuid4().hex+suffix
  file.save(UPLOADS/name)
  saved.append((kind,'/static/uploads/'+name))
 return saved

@app.post('/admin/product')
@admin_only
def save_product():
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 title=request.form.get('title','').strip();slug=request.form.get('slug','').strip().lower();description=request.form.get('description','').strip();image=request.form.get('image','').strip()
 price=parse_money(request.form.get('price','0'))
 category=request.form.get('category','').strip()[:80]
 cost_raw=request.form.get('supplier_cost','').strip()
 supplier_cost=parse_money(cost_raw) if cost_raw else None
 if image and not image.startswith('https://'):abort(400,'HTTPS images only')
 if not title or not re.fullmatch('[a-z0-9]+(?:-[a-z0-9]+)*',slug):abort(400)
 options={}
 for label,field in [('المقاس','sizes'),('اللون','colors')]:
  vals=list(dict.fromkeys(v.strip() for v in request.form.get(field,'').split(',') if v.strip()))
  if len(vals)>30 or any(len(v)>48 for v in vals):abort(400,'Invalid options')
  if vals:options[label]=vals
 files=request.files.getlist('media')
 with db() as c:
  if c.execute('SELECT 1 FROM products WHERE slug=?',(slug,)).fetchone():flash('رابط المنتج مستخدم بالفعل');return redirect('/admin')
  saved=store_uploads(files)
  try:
   cur=c.execute('INSERT INTO products(title,slug,description,price,image,options_json,category,supplier_cost) VALUES(?,?,?,?,?,?,?,?)',(title,slug,description,price,next((path for kind,path in saved if kind=='image'),image),json.dumps(options,ensure_ascii=False),category,supplier_cost))
   for i,(kind,path) in enumerate(saved):c.execute('INSERT INTO product_media(product_id,kind,path,position) VALUES(?,?,?,?)',(cur.lastrowid,kind,path,i))
  except Exception:
   for _,path in saved:(ROOT/path.lstrip('/')).unlink(missing_ok=True)
   raise
 return redirect('/admin')

@app.post('/admin/product/<int:pid>/media')
@admin_only
def add_media(pid):
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 with db() as c:
  product=c.execute('SELECT source FROM products WHERE id=?',(pid,)).fetchone()
  if not product:abort(404)
  if product['source']=='safka':abort(403,'Supplier-managed media is read-only')
  saved=store_uploads(request.files.getlist('media'))
  offset=c.execute('SELECT COALESCE(MAX(position),-1)+1 FROM product_media WHERE product_id=?',(pid,)).fetchone()[0]
  for i,(kind,path) in enumerate(saved):
   c.execute('INSERT INTO product_media(product_id,kind,path,position) VALUES(?,?,?,?)',(pid,kind,path,offset+i))
   if kind=='image' and not c.execute('SELECT image FROM products WHERE id=?',(pid,)).fetchone()['image']:c.execute('UPDATE products SET image=? WHERE id=?',(path,pid))
 return redirect('/admin')

@app.post('/admin/product/<int:pid>/edit')
@admin_only
def edit_product(pid):
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 title=request.form.get('title','').strip()[:180]
 desc=request.form.get('description','').strip()[:5000]
 category=request.form.get('category','').strip()[:80]
 raw_cost=request.form.get('supplier_cost','').strip()
 supplier_cost=parse_money(raw_cost) if raw_cost else None
 price=parse_money(request.form.get('price','0'))
 if not title:abort(400)
 options={}
 for label,field in [('المقاس','sizes'),('اللون','colors')]:
  vals=list(dict.fromkeys(x.strip() for x in request.form.get(field,'').split(',') if x.strip()))
  if len(vals)>30 or any(len(x)>48 for x in vals):abort(400)
  if vals:options[label]=vals
 with db() as c:
  product=c.execute('SELECT source FROM products WHERE id=?',(pid,)).fetchone()
  if not product:abort(404)
  if product['source']=='safka':abort(403,'Supplier-managed price and data cannot be edited manually')
  c.execute('UPDATE products SET title=?,description=?,price=?,options_json=?,category=?,supplier_cost=? WHERE id=?',(title,desc,price,json.dumps(options,ensure_ascii=False),category,supplier_cost,pid))
 flash('تم تحديث المنتج')
 return redirect('/admin#products')

@app.post('/admin/product/<int:pid>/media/<int:mid>/delete')
@admin_only
def delete_media(pid,mid):
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 with db() as c:
  m=c.execute('SELECT * FROM product_media WHERE product_id=? AND id=?',(pid,mid)).fetchone()
  if not m:abort(404)
  product=c.execute('SELECT source FROM products WHERE id=?',(pid,)).fetchone()
  if product and product['source']=='safka':abort(403,'Supplier media is managed by sync')
  c.execute('DELETE FROM product_media WHERE id=?',(mid,))
  if c.execute('SELECT image FROM products WHERE id=?',(pid,)).fetchone()['image']==m['path']:
   first=c.execute("SELECT path FROM product_media WHERE product_id=? AND kind='image' ORDER BY position,id LIMIT 1",(pid,)).fetchone()
   c.execute('UPDATE products SET image=? WHERE id=?',((first['path'] if first else ''),pid))
 # Owned upload directory only, never arbitrary external paths.
 if m['path'].startswith('/static/uploads/'):(ROOT/m['path'].lstrip('/')).unlink(missing_ok=True)
 return redirect('/admin#products')

@app.post('/admin/product/<int:pid>/toggle')
@admin_only
def toggle(pid):
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 with db() as c:
  product=c.execute('SELECT source FROM products WHERE id=?',(pid,)).fetchone()
  if not product:abort(404)
  if product['source']=='safka':abort(403,'Supplier stock/visibility is controlled by sync')
  c.execute('UPDATE products SET active=1-active WHERE id=?',(pid,))
 return redirect('/admin')
@app.post('/admin/shipping')
@admin_only
def shipping():
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 gov=request.form.get('governorate','').strip()
 fee=parse_money(request.form.get('fee',''))
 raw=request.form.get('supplier_cost','').strip()
 supplier_cost=parse_money(raw) if raw else None
 if not gov or fee<0:abort(400)
 with db() as c:c.execute('INSERT INTO shipping(governorate,fee,supplier_cost) VALUES(?,?,?) ON CONFLICT(governorate) DO UPDATE SET fee=excluded.fee,supplier_cost=excluded.supplier_cost',(gov,fee,supplier_cost))
 return redirect('/admin')
@app.post('/admin/order/<int:oid>/status')
@admin_only
def status(oid):
 value=request.form.get('status','');allowed=('new','confirmed','preparing','shipped','delivered','cancelled')
 if value not in allowed:abort(400)
 with db() as c:c.execute('UPDATE orders SET status=? WHERE id=?',(value,oid))
 return redirect('/admin')
@app.post('/admin/shipping-zone')
@admin_only
def edit_shipping_zone():
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 gov=request.form.get('governorate','').strip()[:80]
 city=request.form.get('city','').strip()[:80]
 area=request.form.get('area','').strip()[:80]
 if not gov or not city or (area and not city):abort(400)
 fee=parse_money(request.form.get('fee',''))
 raw=request.form.get('supplier_cost','').strip()
 cost=parse_money(raw) if raw else None
 with db() as conn:
  if not conn.execute('SELECT 1 FROM shipping WHERE governorate=?',(gov,)).fetchone():abort(400,'Set a governorate rate first')
  conn.execute('''INSERT INTO shipping_zones(governorate,city,area,fee,supplier_cost)
VALUES(?,?,?,?,?) ON CONFLICT(governorate,city,area) DO UPDATE SET fee=excluded.fee,supplier_cost=excluded.supplier_cost''',(gov,city,area,fee,cost))
 flash('تم تحديث تسعير المنطقة')
 return redirect('/admin#shipping')

@app.post('/admin/shipping-zone/<int:zone_id>/delete')
@admin_only
def delete_shipping_zone(zone_id):
 if safka_only_mode():abort(403,'المنتجات والشحن يتم تحديثها من صفقة فقط')
 with db() as conn:conn.execute('DELETE FROM shipping_zones WHERE id=?',(zone_id,))
 return redirect('/admin#shipping')

@app.post('/admin/contact')
@admin_only
def update_contact():
 phone=request.form.get('support_phone','').strip()
 email=request.form.get('support_email','').strip().lower()
 if phone and not re.fullmatch(r'01[0125]\d{8}',phone):abort(400,'Invalid support phone')
 if email and not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):abort(400,'Invalid contact email')
 with db() as conn:
  for k,v in {'support_phone':phone,'support_email':email}.items():
   conn.execute('INSERT INTO store_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v))
 flash('تم حفظ معلومات الدعم')
 return redirect('/admin#integrations')

@app.post('/compare/add/<int:pid>')
def compare_add(pid):
 with db() as conn:p=conn.execute('SELECT id FROM products WHERE id=? AND active=1',(pid,)).fetchone()
 if p is None:abort(404)
 selected=[int(x) for x in session.get('compare_ids',[]) if str(x).isdigit()]
 if pid not in selected:
  if len(selected)>=3:flash('يمكن مقارنة 3 منتجات كحد أقصى')
  else:selected.append(pid)
 session['compare_ids']=selected
 return redirect(request.referrer if request.referrer and request.referrer.startswith(request.host_url) else '/compare')

@app.post('/compare/remove/<int:pid>')
def compare_remove(pid):
 session['compare_ids']=[int(x) for x in session.get('compare_ids',[]) if str(x).isdigit() and int(x)!=pid]
 return redirect('/compare')

@app.get('/compare')
def compare_view():
 ids=[int(x) for x in session.get('compare_ids',[]) if str(x).isdigit()][:3]
 with db() as conn:
  products=[conn.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone() for pid in ids]
 products=[p for p in products if p is not None]
 return render_template('compare.html',products=products)

# Transparent, rule-based guided finder. Never invent items or call an unconfigured AI provider.
AR_SYNONYMS={
 'هديه':['هدية','هديه','gift','مناسبة'],
 'سماعه':['سماعة','سماعه','headphone','headset','bluetooth','صوت'],
 'موبايل':['هاتف','موبايل','تليفون','phone','mobile'],
 'لبس':['ملابس','تيشيرت','قميص','جلباب','تي شيرت','cloth','shirt','tee'],
 'شنطه':['شنطة','شنطه','حقيبة','bag'],
 'رياضه':['رياضة','رياضه','جيم','sport','gym'],
}
def normalized_terms(text):
 text=re.sub(r'[إأآ]','ا',text.lower())
 text=text.replace('ة','ه').replace('ى','ي')
 return set(re.findall(r'[\w\u0600-\u06FF]{2,}',text))

def score_products(rows,query='',budget=None,category=''):
 query_tokens=normalized_terms(query)
 expanded=set(query_tokens)
 for words in AR_SYNONYMS.values():
  if query_tokens & normalized_terms(' '.join(words)):expanded|=normalized_terms(' '.join(words))
 results=[]
 for p in rows:
  if budget is not None and p['price']>budget:continue
  if category and p['category']!=category:continue
  title=normalized_terms(p['title']); desc=normalized_terms(p['description']);cat=normalized_terms(p['category'])
  matches=expanded&(title|desc|cat)
  score=len(expanded&title)*4+len(expanded&cat)*3+len(expanded&desc)
  if expanded and score==0:continue
  results.append((score,p))
 return [p for _,p in sorted(results,key=lambda row:(-row[0],row[1]['price'],row[1]['id']))[:8]]

def local_ai_recommend(candidates,query,budget,category):
 """Optional local Ollama mode; no customer data leaves machine. Strictly verify IDs."""
 if os.environ.get('SHOPY_ASSISTANT_ENGINE','rules')!='ollama' or not query:return None
 ids={int(p['id']) for p in candidates}
 if not ids:return []
 catalog=[{'id':p['id'],'name':p['title'][:120],'category':p['category'][:200],
           'description':p['description'][:250],'price_piastres':p['price']} for p in candidates[:80]]
 prompt=('You are a shopping assistant. Reply with JSON ONLY, containing key "ids" as a list of up to 8 distinct product IDs from the catalog. '
         'Do not invent products or override product prices. Choose relevant products. '
         'Customer request: '+query+'; category: '+category+'; budget in piastres: '+str(budget)+'; catalog: '+json.dumps(catalog,ensure_ascii=False))
 payload=json.dumps({'model':os.environ.get('SHOPY_OLLAMA_MODEL','llama3.2'),
                     'prompt':prompt,'stream':False,'format':'json'}).encode('utf-8')
 try:
  req=urllib.request.Request('http://127.0.0.1:11434/api/generate',data=payload,
                             headers={'Content-Type':'application/json'},method='POST')
  with urllib.request.urlopen(req,timeout=5) as response:
   result=json.loads(response.read(60000))
  parsed=json.loads(result.get('response','{}'))
  chosen=parsed.get('ids',[])
  if not isinstance(chosen,list):return None
  by_id={p['id']:p for p in candidates}
  seen=set();output=[]
  for pid in chosen:
   if isinstance(pid,int) and pid in ids and pid not in seen:
    seen.add(pid);output.append(by_id[pid])
  return output[:8] if output else None
 except (ValueError,TypeError,KeyError,TimeoutError,OSError):return None

@app.route('/assistant',methods=['GET','POST'])
def assistant():
 query=request.values.get('need','').strip()[:180]
 category=request.values.get('category','').strip()[:200]
 raw_budget=request.values.get('budget','').strip()
 budget=None;warning=''
 if raw_budget:
  try:budget=parse_money(raw_budget)
  except Exception:budget=None;warning='الميزانية غير صحيحة'
 with db() as conn:
  products=conn.execute('SELECT * FROM products WHERE active=1').fetchall()
  categories=[r['category'] for r in conn.execute("SELECT DISTINCT category FROM products WHERE active=1 AND category!='' ORDER BY category")]
 matches=score_products(products,query,budget,category) if query or category or budget is not None else []
 engine='rules'
 # We only send non-personal product details + user query to the local Ollama server, if manually enabled.
 if os.environ.get('SHOPY_ASSISTANT_ENGINE')=='ollama' and query:
  allowed=[p for p in products if (budget is None or p['price']<=budget) and (not category or p['category']==category)]
  refined=local_ai_recommend(allowed,query,budget,category)
  if refined is not None:matches=refined;engine='ollama'
 return render_template('assistant.html',matches=matches,need=query,budget=raw_budget,category=category,categories=categories,warning=warning,asked=request.method=='POST',engine=engine)


@app.get('/admin/integrations')
@admin_only
def integrations_redirect():return redirect('/admin#integrations')

@app.route('/robots.txt')
def robots():return app.response_class('User-agent: *\nDisallow: /admin\nDisallow: /account\nDisallow: /checkout\nDisallow: /cart\nDisallow: /compare\nDisallow: /assistant\nDisallow: /order\n',mimetype='text/plain')
@app.route('/sitemap.xml')
def sitemap():
 from xml.sax.saxutils import escape
 with db() as c:paths=[p['slug'] for p in c.execute('SELECT slug FROM products WHERE active=1')]
 base=request.url_root.rstrip('/')
 xml='<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+''.join('<url><loc>'+escape(base+'/products/'+slug)+'</loc></url>' for slug in paths)+'</urlset>'
 return app.response_class(xml,mimetype='application/xml')
@app.route('/health')
def health():return jsonify(ok=True)
@app.errorhandler(413)
def too_large(e):return 'الملفات أكبر من الحد المسموح (50MB للطلب)',413

if __name__=='__main__':
 init();app.run(host='127.0.0.1',port=5000,debug=False)
