import os, sqlite3, secrets, hashlib, hmac, json, re, time, uuid, urllib.request
from decimal import Decimal, InvalidOperation
from werkzeug.utils import secure_filename
from pathlib import Path
from functools import wraps
from flask import Flask, request, redirect, session, render_template, abort, flash, jsonify, g, has_request_context
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from catalog_quality import clean_description
from profit_pricing import init_pricing_schema, checkout_offer, displayed_product_price, bundle_enabled
from social_auth import init_social_schema, configured_providers, start_flow, exchange_identity, resolve_account, SocialAuthError
from ui_queries import paginate, price_filter, order_filters, grouped, ORDER_LABELS
from safka_status import SUPPLIER_STATUS_LABELS, apply_order_status_event
from urllib.parse import urlencode, unquote
from zoneinfo import ZoneInfo
from datetime import datetime
from promo_codes import (init_promo_schema, create_code, get_active_code, calculate_discount,
                         quote_promo, validate_settings, KIND_LABELS,
                         consume_code, normalize_code, PromoError)
from shopy_notify import init_notification_store, queue_new_order, configured_recipients
from safka_bridge import init_safka_schema, quote_safka_shipping, shipping_pricing_document_id, queue_safka_order, supplier_stock_ready, migrate_supplier_product_slugs, CatalogValidationError
from safka_dry_run import prepare_dry_run
from safka_live_orders import (canonical_order, load_contract, build_payload,
    submit_payload, claim_one_test_order, record_outcome, OrderContractError)
from safka_cleanup import summary as safka_cleanup_summary, cleanup as safka_cleanup, CleanupNotReady, CONFIRM_TEXT
ROOT=Path(__file__).parent
ASSET_VERSION=hashlib.sha256((ROOT/'static/style.css').read_bytes()+(ROOT/'static/store.js').read_bytes()).hexdigest()[:12]
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
  init_social_schema(c)
  init_pricing_schema(c)
  init_promo_schema(c)
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
  migrate_supplier_product_slugs(c)
  c.executescript("""CREATE TABLE IF NOT EXISTS favorites(
     user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
     created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
     PRIMARY KEY(user_id,product_id));
  CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
  CREATE TABLE IF NOT EXISTS product_reviews(
     id INTEGER PRIMARY KEY,
     product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
     user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
     rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
     comment TEXT NOT NULL DEFAULT '',
     created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
     updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
     UNIQUE(product_id,user_id),
     CHECK(rating > 3 OR length(trim(comment))>=10));
  CREATE INDEX IF NOT EXISTS idx_reviews_product ON product_reviews(product_id,created_at);""")
  if os.environ.get('DATABASE_URL'):
   c.execute('ALTER TABLE favorites ENABLE ROW LEVEL SECURITY')
   c.execute('ALTER TABLE product_reviews ENABLE ROW LEVEL SECURITY')
  if os.environ.get('DATABASE_URL'):
   # Keep Supabase Data API anonymous/authorized roles away from merchant edits.
   c.execute('ALTER TABLE product_overrides ENABLE ROW LEVEL SECURITY')
  for name,table,columns in [
   ('idx_products_price_page','products','price,id'),
   ('idx_products_category_price','products','category,price,id'),
   ('idx_orders_status_page','orders','status,id'),
   ('idx_orders_governorate_page','orders','governorate,id'),
   ('idx_orders_created_page','orders','created_at,id'),
   ('idx_orders_user_page','orders','user_id,id'),
   ('idx_order_items_order','order_items','order_id'),
   ('idx_product_media_product','product_media','product_id,position,id')]:
   c.execute('CREATE INDEX IF NOT EXISTS '+name+' ON '+table+'('+columns+')')
  # Never auto-seed demonstration catalog or arbitrary shipping prices.
  # User-owned records remain until an explicit backup + cleanup operation.
def csrf():return session.setdefault('csrf',secrets.token_urlsafe(24))
@app.template_filter('product_description')
def product_description(value):return clean_description(value)

@app.template_filter('options_from_json')
def options_from_json(value):
 try:return json.loads(value or '{}')
 except (ValueError,TypeError):return {}

def supplier_checkout_live():
 from safka_auto_orders import live_enabled
 return live_enabled()

def safka_only_mode():
 return store_settings().get('catalog_mode')=='safka_only'

@app.context_processor
def context():
 return {'csrf':csrf(),'user':current_user(),'cart_count':sum(q for q in session.get('cart',{}).values() if isinstance(q,int)),'money':lambda x:f'{x/100:,.2f}', 'contact':store_settings(),'staging_mode':STAGING_MODE and not supplier_checkout_live()}
def current_user():
 if hasattr(g,'store_user'):return g.store_user
 uid=session.get('uid')
 if not uid:g.store_user=None;return None
 with db() as c:g.store_user=c.execute('SELECT id,email,role FROM users WHERE id=?',(uid,)).fetchone()
 return g.store_user
@app.before_request
def staging_gate():
 if STAGING_MODE:
  private_password=os.environ.get('SHOPY_STAGING_PASSWORD','')
  if STAGING_BASIC_AUTH_ENABLED and private_password and request.path!='/health':
   auth=request.authorization
   if not auth or not hmac.compare_digest(auth.password or '',private_password):
    return app.response_class('Staging access only',401,{'WWW-Authenticate':'Basic realm="Shopy Staging"'})
  # Customer accounts work independently of supplier checkout. A shopper must log in to order.
  if request.path.startswith('/checkout') and request.method=='POST' and not supplier_checkout_live():
   # Public checkout requires a verified supplier contract, property mapping,
   # approved commission policy, and an explicit production launch flag.
   owner = current_user() if os.getenv('SHOPY_STAGING_OWNER_CHECKOUT') == '1' else None
   if not owner or owner['role'] != 'admin':
    abort(503,'Orders are temporarily unavailable during supplier setup')

@app.after_request
def noindex_staging(resp):
 if request.path.startswith(('/auth/','/login','/register','/account')):
  resp.headers['Cache-Control']='no-store'
  resp.headers['Referrer-Policy']='no-referrer'
 if STAGING_MODE and not supplier_checkout_live():
  resp.headers['X-Robots-Tag']='noindex, nofollow'
  resp.headers['Cache-Control']='no-store' if request.path.startswith(('/admin','/account','/login')) else 'private, no-cache'
 return resp

@app.before_request
def protect():
 if request.method in ('POST','PUT','PATCH','DELETE'):
  # Provider POSTs have no browser session/CSRF. Their separate opaque URL
  # tokens must be verified inside the webhook handler before accepting data.
  if request.path.startswith('/integrations/safka/hook/'):
   return
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
 if not has_request_context():
  with db() as conn:return {r['key']:r['value'] for r in conn.execute('SELECT key,value FROM store_settings')}
 if not hasattr(g,'store_settings'):
  with db() as conn:g.store_settings={r['key']:r['value'] for r in conn.execute('SELECT key,value FROM store_settings')}
 return g.store_settings

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

def customer_favorite_ids(conn,user):
 if not user:return {x for x in session.get('guest_favorites',[]) if isinstance(x,int) and x>0}
 return {r['product_id'] for r in conn.execute('SELECT product_id FROM favorites WHERE user_id=?',(user['id'],)).fetchall()}

def customer_product_rating_map(conn,ids=None):
 if ids is not None and not ids:return {}
 where=(' WHERE product_id IN ('+','.join('?' for _ in ids)+')') if ids is not None else ''
 return {r['product_id']:{'avg':float(r['avg_rating']),'count':r['review_count']}
         for r in conn.execute('SELECT product_id,AVG(rating) AS avg_rating,COUNT(*) AS review_count FROM product_reviews'+where+' GROUP BY product_id',ids or []).fetchall()}

def page_link(param,page,anchor=''):
 args=request.args.to_dict();args[param]=page
 return request.path+'?'+urlencode(args)+('#'+anchor if anchor else '')

def query_link(**updates):
 args=request.args.to_dict()
 for key,value in updates.items():
  if value in ('',None):args.pop(key,None)
  else:args[key]=value
 return request.path+('?' + urlencode(args) if args else '')+'#catalog'

@app.context_processor
def ui_helpers():
 return dict(asset_version=ASSET_VERSION,page_link=page_link,query_link=query_link,order_labels=ORDER_LABELS,
             can_order=supplier_stock_ready,product_choices=product_choices,shown_price=displayed_product_price,bundle_active=bundle_enabled(),
             supplier_status_labels=SUPPLIER_STATUS_LABELS,supplier_live=supplier_checkout_live())

def safe_back_url():
 target=request.form.get('return_to','')
 if not isinstance(target,str) or not target.startswith('/') or target.startswith('//') or '\\' in target:
  return '/favorites'
 # Never let a caller push the browser onto a different domain or a protocol-relative path.
 if target.startswith('/admin') or target.startswith('/integrations'):
  return '/favorites'
 return target[:250]


@app.route('/')
def home():
 capture_campaign()
 term=request.args.get('q','').strip()[:80]; category=request.args.get('category','').strip()[:200]
 filters=["(active=1 OR (source='safka' AND supplier_listed=1))"];args=[]
 if category:filters.append('category=?');args.append(category)
 try:
  low=price_filter(request.args.get('min_price',''));high=price_filter(request.args.get('max_price',''))
  if low is not None and high is not None and low>high:raise ValueError('السعر من أكبر من السعر إلى؛ راجع النطاق.')
 except ValueError as exc:
  flash(str(exc));low=high=None
 if bundle_enabled():
  low=max(0,low-9500) if low is not None else None
  high=high-9500 if high is not None else None
 for amount,operator in [(low,'>='),(high,'<=')]:
  if amount is not None:filters.append('price'+operator+'?');args.append(amount)
 sort=request.args.get('sort','relevance' if term else 'newest')
 order={'newest':'id DESC','price_low':'price ASC,id DESC','price_high':'price DESC,id DESC'}.get(sort,'id DESC')
 with db() as c:
  where=' WHERE '+' AND '.join(filters)
  if term:
   # Small verified catalog (~500 products): rank filtered rows by name first.
   # No sync/network calls and no supplier side effects on customer searches.
   from catalog_search import search_ranked,page_ranked
   candidates=c.execute('SELECT * FROM products'+where+' ORDER BY id DESC',args).fetchall()
   items,pager=page_ranked(search_ranked(candidates,term,sort),request.args.get('page'))
  else:
   items,pager=paginate(c,'SELECT * FROM products'+where,'SELECT COUNT(*) FROM products'+where,args,order,request.args.get('page'),30,anchor='catalog')
  categories=[r['category'] for r in c.execute("SELECT DISTINCT category FROM products WHERE (active=1 OR (source='safka' AND supplier_listed=1)) AND category!='' ORDER BY category")]
  fav_ids=customer_favorite_ids(c,current_user())
  ratings_map=customer_product_rating_map(c,[p['id'] for p in items])
 return render_template('home.html',items=items,term=term,category=category,categories=categories,
                        fav_ids=fav_ids,ratings_map=ratings_map,pager=pager)

def product_slug_variants(slug):
 """Recover exact UTF-8 slugs if an upstream proxy forwards percent encoding.

 Vercel can pass /products/%D8... literally to the Flask WSGI route.
 Try direct + strict UTF-8 decoding, never fuzzy title or ID lookups.
 """
 candidates=[slug]
 # Some gateways may double-encode percent escapes.
 for _ in range(2):
  try:decoded=unquote(candidates[-1], encoding='utf-8', errors='strict')
  except (UnicodeDecodeError,ValueError):break
  if decoded==candidates[-1] or any(ch in decoded for ch in ('/',chr(0),'?','#')):break
  if len(decoded)>250:break
  candidates.append(decoded)
 # WSGI gateways sometimes decode UTF-8 bytes as Latin-1.
 for val in tuple(candidates):
  if any(ord(ch) in range(128,256) for ch in val):
   try:repaired=val.encode('latin-1').decode('utf-8')
   except (UnicodeError,ValueError):continue
   if repaired not in candidates and len(repaired)<=250 and not any(ch in repaired for ch in ('/',chr(0),'?','#')):
    candidates.append(repaired)
 return tuple(dict.fromkeys(candidates))


@app.route('/products/<slug>')
def product(slug):
 capture_campaign()
 with db() as c:
  variants=product_slug_variants(slug)
  p=None
  for candidate in variants:
   p=c.execute("SELECT * FROM products WHERE slug=? AND (active=1 OR (source='safka' AND supplier_listed=1))",(candidate,)).fetchone()
   if p:break
  if not p:
   for candidate in variants:
    alias=c.execute('SELECT product_id FROM product_slug_aliases WHERE slug=?',(candidate,)).fetchone()
    if alias:
     moved=c.execute("SELECT slug FROM products WHERE id=? AND (active=1 OR (source='safka' AND supplier_listed=1))",(alias['product_id'],)).fetchone()
     if moved:return redirect('/products/'+moved['slug'],code=301)
   abort(404)
  others=c.execute("SELECT * FROM products WHERE (active=1 OR (source='safka' AND supplier_listed=1)) AND id!=? ORDER BY CASE WHEN category=? AND category!='' THEN 0 ELSE 1 END,id DESC LIMIT 4",(p['id'],p['category'])).fetchall()
 with db() as c:
  media=c.execute('SELECT * FROM product_media WHERE product_id=? ORDER BY position,id',(p['id'],)).fetchall()
  user=current_user()
  fav_ids=customer_favorite_ids(c,user)
  ratings_map=customer_product_rating_map(c,[p['id']]+[x['id'] for x in others])
  reviews=c.execute("""SELECT rating,comment,created_at FROM product_reviews
                   WHERE product_id=? ORDER BY created_at DESC,id DESC LIMIT 50""",(p['id'],)).fetchall()
  mine=(c.execute('SELECT rating,comment FROM product_reviews WHERE product_id=? AND user_id=?',
         (p['id'],user['id'])).fetchone() if user else None)
 return render_template('product.html',p=p,others=others,media=media,
                        options=json.loads(p['options_json'] or '{}'),stock_ready=supplier_stock_ready(p),
                        fav_ids=fav_ids,ratings_map=ratings_map,reviews=reviews,mine=mine)
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

def action_response(message,ok=True,redirect_to='/cart',status=200,**extra):
 if request.headers.get('Accept','').startswith('application/json'):
  return jsonify(ok=ok,message=message,cart_count=sum(q for q in session.get('cart',{}).values() if isinstance(q,int)),**extra),status
 flash(message);return redirect(redirect_to)

@app.post('/cart/add/<int:pid>')
def add_cart(pid):
 with db() as c:p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
 if not p:abort(404)
 options=product_choices(p);chosen={}
 for key,values in options.items():
  val=request.form.get('option_'+key,'')
  if val not in values:return action_response('اختار '+key+' قبل الإضافة للسلة',False,'/products/'+p['slug'],422)
  chosen[key]=val
 cart=session.get('cart',{});key=encoded_cart_key(pid,chosen)
 new_quantity=min(99,int(cart.get(key,0))+1)
 combined=sum(q for k,q in cart.items() if decoded_cart_key(str(k))[0]==pid and isinstance(q,int) and k!=key)+new_quantity
 if p['source']=='safka' and not supplier_stock_ready(p,combined):
  return action_response('الكمية المطلوبة غير متوفرة حاليًا؛ اختار كمية أقل',False,'/products/'+p['slug'],409)
 cart[key]=new_quantity;session['cart']=cart
 back=safe_back_url() if request.form.get('return_to') else '/cart'
 return action_response('تمت إضافة المنتج للسلة',redirect_to=back,view_url='/cart')

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


def cart_promo_state():
 code=session.get('promo_code','')
 if not code:return None
 with db() as conn:
  try:
   row=get_active_code(conn,code)
   return {'code':row['code'],'percent':row['percent'],'kind':row['kind'],
           'kind_label':KIND_LABELS.get(row['kind'],'خصم'),
           'amount_piastres':row['amount_piastres'],'min_items':row['min_items'],
           'max_discounted_items':row['max_discounted_items'],'active':True}
  except PromoError as exc:return {'code':str(code)[:32],'percent':0,'active':False,'message':str(exc)}


@app.get('/promo-code')
def customer_promo_page():
 return render_template('promo_customer.html',promo_state=cart_promo_state())


@app.post('/promo-code/apply')
def apply_customer_promo():
 action=request.form.get('action','apply')
 dest=request.form.get('return_to','/cart')
 if dest not in ('/cart','/checkout','/promo-code'):dest='/cart'
 wants_json='application/json' in request.headers.get('Accept','')
 if action=='remove':
  session.pop('promo_code',None)
  message='تم إزالة كود الخصم.'
  if wants_json:return jsonify(ok=True,message=message,promo_state=None)
  flash(message)
  return redirect(dest)
 if action!='apply':
  message='عملية كود الخصم غير صحيحة.'
  if wants_json:return jsonify(ok=False,message=message),400
  flash(message)
  return redirect(dest)
 try:
  with db() as conn:promo=get_active_code(conn,request.form.get('promo_code',''))
  session['promo_code']=promo['code']
  message='تم تطبيق كود الخصم. هيتحسب الإجمالي الجديد بعد التوصيل.'
  if wants_json:return jsonify(ok=True,message=message,promo_state=cart_promo_state())
  flash(message)
 except PromoError as exc:
  if wants_json:return jsonify(ok=False,message=str(exc),promo_state=cart_promo_state()),422
  flash(str(exc))
 return redirect(dest)


@app.route('/cart',methods=['GET','POST'])
def cart():
 if request.method=='POST':return change_cart_quantity()
 return render_template('cart.html',rows=cart_data(),promo_state=cart_promo_state())

@app.post('/cart/quantity')
def change_cart_quantity():
 key=request.form.get('key','')
 action=request.form.get('action','')
 cart=session.get('cart',{})
 wants_json='application/json' in request.headers.get('Accept','')
 def error(message,status=400):
  if wants_json:return jsonify(ok=False,message=message),status
  if status in (400,404):abort(status,message)
  flash(message)
  return redirect('/cart')
 if key not in cart:return error('المنتج غير موجود في السلة')
 pid,_=decoded_cart_key(key)
 previous=cart[key]
 if pid is None or type(previous) is not int or not 1<=previous<=99:return error('بيانات السلة غير صحيحة')
 if action not in ('increase','decrease'):return error('اختيار تعديل الكمية غير صحيح')
 if action=='decrease':
  if previous==1:cart.pop(key)
  else:cart[key]=previous-1
 else:
  if previous>=99:return error('الحد الأقصى 99 قطعة من نفس الاختيار',422)
  with db() as c:p=c.execute('SELECT * FROM products WHERE id=? AND active=1',(pid,)).fetchone()
  if not p:return error('المنتج غير متاح حاليًا',404)
  combined=previous+1+sum(q for k,q in cart.items() if k!=key and type(q) is int and decoded_cart_key(str(k))[0]==pid)
  if p['source']=='safka' and not supplier_stock_ready(p,combined):
   return error('الكمية المطلوبة غير متوفرة حاليًا؛ اختار كمية أقل',409)
  cart[key]=previous+1
 session['cart']=cart
 if wants_json:
  return jsonify(ok=True,cart_count=sum(cart.values()),
                 cart_html=render_template('cart.html',rows=cart_data(),promo_state=cart_promo_state()))
 return redirect('/cart')

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
  try:
   offer=checkout_offer(conn,rows,quoted)
   promo=quote_promo(conn,session.get('promo_code',''),offer,rows,quoted)
  except (ValueError,PromoError) as exc:return jsonify(ok=False,message=str(exc)),409
 quote_data=[request.args.get(k,'').strip() for k in ('governorate','city','area')]
 quote_data += [offer['fee'],offer['displayed_subtotal'],offer['bundle_discount'],offer['bundle_offer_version'],session.get('cart',{}),promo['code'],promo['product_discount'],promo['shipping_discount']]
 ticket=URLSafeTimedSerializer(app.secret_key,salt='checkout-quote-v1').dumps(quote_data)
 return jsonify(ok=True,products_total_piastres=offer['displayed_subtotal'],
                merchandise_total_piastres=offer['merchandise_total'],
                bundle_discount_piastres=offer['bundle_discount'],
                promo_code=promo['code'],promo_kind=promo['kind'],promo_percent=promo['percent'],
                promo_discount_piastres=promo['product_discount'],
                promo_shipping_discount_piastres=promo['shipping_discount'],
                bundle_unit_count=offer['bundle_unit_count'],
                shipping_piastres=offer['fee']-promo['shipping_discount'],
                standard_shipping_piastres=(offer['fee'] if offer['bundle_offer_version']==3 else offer['standard']),
                shipping_discount_piastres=((0 if offer['bundle_offer_version']==3 else offer['discount'])+promo['shipping_discount']),
                quote_token=ticket)

@app.get('/api/shipping-governorates')
def safka_shipping_governorates():
 # Serve the last owner/overnight-verified snapshot. Customer GETs never sync.
 with db() as conn:
  rates=conn.execute('SELECT DISTINCT governorate FROM safka_shipping_rates ORDER BY governorate').fetchall()
 return jsonify(ok=True,governorates=[r['governorate'] for r in rates])

@app.get('/api/shipping-cities')
def safka_shipping_cities():
 governorate=request.args.get('governorate','').strip()[:80]
 if not governorate:return jsonify(cities=[])
 with db() as conn:
  rows=conn.execute("SELECT DISTINCT city FROM safka_shipping_rates WHERE governorate=? AND city!='' ORDER BY city",(governorate,)).fetchall()
 return jsonify(cities=[r['city'] for r in rows][:1000])


def remember_order_ref(ref):
 # Reassign rather than mutating a nested session list, so Flask saves it.
 refs=[item for item in session.get('my_order_refs',[]) if isinstance(item,str) and item!=ref]
 session['my_order_refs']=(refs+[ref])[-10:]


@app.route('/checkout',methods=['GET','POST'])
def checkout():
 # Guest checkout: identity is optional; contact/delivery details are required.
 # Orders retain a nullable user_id and guests receive session-scoped confirmation.
 rows=cart_data()
 if not rows:return redirect('/cart')
 if not session.get('checkout_key'):session['checkout_key']=secrets.token_urlsafe(24)
 supplier_cart=any(p['source']=='safka' for p,_,_,_ in rows)
 mixed_cart=supplier_cart and any(p['source']!='safka' for p,_,_,_ in rows)
 # Customer visits use the stable shipping snapshot, not supplier API sync.
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
   return render_template('checkout.html',rows=rows,rates=rates,supplier_cart=supplier_cart,
                        checkout_ready=(supplier_checkout_live() or not STAGING_MODE),promo_state=cart_promo_state())
  key=session['checkout_key']
  checked_details={}
  if supplier_cart and supplier_checkout_live():
   from safka_auto_orders import verify_checkout_products
   try:
    checked_details=verify_checkout_products(rows)
   except (OrderContractError,CatalogValidationError,ValueError,OSError,TypeError,KeyError):
    flash('تعذر تأكيد توفر أحد المنتجات أو تغيّر سعره لدى المورد. لم يتم إرسال طلب؛ راجع السلة وحاول مرة أخرى.')
    return redirect('/cart')
  try:
   with db() as c:
    c.execute('BEGIN IMMEDIATE')
    existing=c.execute('SELECT reference FROM orders WHERE checkout_key=?',(key,)).fetchone()
    if existing:
     remember_order_ref(existing['reference'])
     return redirect('/order/success/'+existing['reference'])
    if mixed_cart:raise ValueError('المنتجات دي تحتاج طلبات منفصلة؛ راجع السلة')
    rate=(quote_safka_shipping if supplier_cart else shipping_quote)(c,gov,city,area)
    if rate is None:raise ValueError('الشحن غير متاح للعنوان المحدد حاليًا. اختر منطقة مدعومة.')
    fresh=[]
    quantities_by_product={}
    for p,q,choices,_ in rows:
     quantities_by_product[p['id']]=quantities_by_product.get(p['id'],0)+q
    for p,q,choices,_ in rows:
     p2=c.execute('SELECT * FROM products WHERE id=? AND active=1',(p['id'],)).fetchone()
     if not p2 or any(choices.get(k) not in vals for k,vals in product_choices(p2).items()) or set(choices)!=set(product_choices(p2)):
      raise ValueError('بعض المنتجات أو الخيارات لم تعد متاحة، راجع السلة')
     if p2['source']=='safka' and not supplier_stock_ready(p2,quantities_by_product[p2['id']]):
      raise ValueError('الكمية المتاحة غير مؤكدة أو لا تكفي لهذا المنتج؛ راجع السلة')
     fresh.append((p2,q,choices))
    offer=checkout_offer(c,fresh,rate)
    if supplier_cart and supplier_checkout_live():
     try:
      quoted=URLSafeTimedSerializer(app.secret_key,salt='checkout-quote-v1').loads(request.form.get('quote_token',''),max_age=1800)
     except (BadSignature,SignatureExpired):
      raise ValueError('حدّث تكلفة التوصيل قبل تأكيد الطلب؛ لم يتم إرسال أي طلب.') from None
     promo=quote_promo(c,session.get('promo_code',''),offer,fresh,rate)
     expected=[gov,city,area,offer['fee'],offer['displayed_subtotal'],offer['bundle_discount'],offer['bundle_offer_version'],session.get('cart',{}),promo['code'],promo['product_discount'],promo['shipping_discount']]
     if quoted!=expected:raise ValueError('تغيّرت السلة أو تكلفة الشحن. راجع الإجمالي وأكد الطلب مرة أخرى.')
    if not (supplier_cart and supplier_checkout_live()):
     promo=quote_promo(c,session.get('promo_code',''),offer,fresh,rate)
    promo_code=promo['code'];promo_discount=promo['product_discount'];promo_shipping_discount=promo['shipping_discount']
    shipping_due=offer['fee']-promo_shipping_discount
    total=offer['merchandise_total']-promo_discount+shipping_due;ref='SH-'+secrets.token_hex(6).upper()
    full_address=' | '.join((city,area,address))
    pricing_doc=(shipping_pricing_document_id(c,gov,city,area,rate[0],rate[1]) if supplier_cart else None)
    city_row=(c.execute('SELECT city_id FROM safka_shipping_rates WHERE governorate=? AND city=? AND area=?',
                       (gov,city,'')).fetchone() if supplier_cart else None)
    supplier_city_id=city_row['city_id'] if city_row else None
    if supplier_cart and supplier_checkout_live() and (not pricing_doc or not supplier_city_id):
     raise ValueError('بيانات التوصيل غير مكتملة حاليًا. لم يتم إرسال الطلب.')
    cur=c.execute('''INSERT INTO orders(reference,user_id,customer,phone,governorate,address,shipping_fee,total,
       checkout_key,utm_source,utm_campaign,shipping_supplier_cost,safka_shipping_pricing_document_id,safka_city_id,shipping_standard_fee,shipping_discount,retained_profit_floor,bundle_discount,bundle_offer_version,bundle_unit_count,
       promo_code,promo_percent,promo_discount,promo_shipping_discount,promo_kind,promo_value,promo_min_items,promo_max_items)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(checkout_key) DO NOTHING''',(ref,session.get('uid'),name,phone,gov,full_address,shipping_due,total,key,
       session.get('utm_source',''),session.get('utm_campaign',''),rate[1],pricing_doc,supplier_city_id,offer['standard'],offer['discount']+promo_shipping_discount,offer['floor'],offer['bundle_discount'],offer['bundle_offer_version'],offer['bundle_unit_count'],
       promo_code,promo['percent'],promo_discount,promo_shipping_discount,promo['kind'],promo['value'],promo['min_items'],promo['max_items']))
    if cur.rowcount==0:
     existing=c.execute('SELECT reference FROM orders WHERE checkout_key=?',(key,)).fetchone()
     remember_order_ref(existing['reference'])
     return redirect('/order/success/'+existing['reference'])
    if promo_code:consume_code(c,promo_code)
    for p,q,choices in fresh:
     c.execute('INSERT INTO order_items(order_id,product_id,title,unit_price,quantity,variant_json,unit_cost) VALUES(?,?,?,?,?,?,?)',(cur.lastrowid,p['id'],p['title'],displayed_product_price(p),q,json.dumps(choices,ensure_ascii=False),p['supplier_cost']))
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
     'products_total_piasters':total-offer['fee'],
     'shipping_piasters':shipping_due,'total_piasters':total,
     'items':[{'name':p['title'],'qty':q,'options':choices} for p,q,choices in fresh],
     'payment_method':'الدفع عند الاستلام',
    },recipients=recipients,enabled=os.getenv('SHOPY_WA_ENABLED')=='1')
    c.commit()
   if supplier_cart and supplier_checkout_live():
    from safka_auto_orders import submit_saved_order_once
    try:
     submit_saved_order_once(db,cur.lastrowid,detail_fetch=lambda key,pid:checked_details[pid])
    except (OrderContractError,CatalogValidationError,ValueError,OSError,KeyError,TypeError) as exc:
     # Customer order is already saved. Preflight failures are NOT supplier acceptance.
     # Never leak the supplier API key, customer PII or transport response.
     with db() as conn:
      conn.execute("""UPDATE safka_order_outbox SET status='preflight_failed',error=?,updated_at=CURRENT_TIMESTAMP
           WHERE order_id=? AND status='awaiting_provider_configuration'""",
           (type(exc).__name__[:80],cur.lastrowid))
  except ValueError as exc:
   flash(str(exc));return render_template('checkout.html',rows=rows,rates=rates,supplier_cart=supplier_cart,
        checkout_ready=(supplier_checkout_live() or not STAGING_MODE),promo_state=cart_promo_state())
  session['cart']={};session.pop('checkout_key',None);session.pop('promo_code',None)
  remember_order_ref(ref)
  return redirect('/order/success/'+ref)
 return render_template('checkout.html',rows=rows,rates=rates,supplier_cart=supplier_cart,
                        checkout_ready=(supplier_checkout_live() or not STAGING_MODE),promo_state=cart_promo_state())

@app.route('/order/success/<ref>')
def order_success(ref):
 with db() as c:
  order=c.execute('SELECT reference,total,shipping_fee,user_id,id,promo_code,promo_percent,promo_discount,promo_shipping_discount FROM orders WHERE reference=?',(ref,)).fetchone()
  if not order:abort(404)
  user=current_user()
  owned=bool(user and order['user_id']==user['id'])
  # Possession of a reference URL alone must never authorize access.
  guest_session=ref in session.get('my_order_refs',[])
  if not (owned or guest_session):abort(404)
  supplier=c.execute('SELECT status FROM safka_order_outbox WHERE order_id=?',(order['id'],)).fetchone()
 return render_template('success.html',ref=ref,order=order,supplier=supplier)

def complete_customer_auth(uid,role):
 """Retain a guest shopper's cart across authentication; rotate session safely.
 Avoid losing cart/order flow when customer must sign in at checkout.
 """
 previous_cart=session.get('cart',{})
 continue_checkout=bool(session.get('checkout_after_login'))
 pending_fav=session.get('pending_favorite')
 guest_favs=session.get('guest_favorites',[])
 utm=(session.get('utm_source',''),session.get('utm_campaign',''))
 session.clear()
 session['uid']=uid
 session['recent_auth_at']=time.time()
 if guest_favs:
  with db() as conn:
   for pid in guest_favs:
    if isinstance(pid,int) and conn.execute("SELECT 1 FROM products WHERE id=? AND (active=1 OR (source='safka' AND supplier_listed=1))",(pid,)).fetchone():
     conn.execute('INSERT INTO favorites(user_id,product_id) VALUES(?,?) ON CONFLICT(user_id,product_id) DO NOTHING',(uid,pid))
 if isinstance(previous_cart,dict):session['cart']=previous_cart
 if utm[0]:session['utm_source']=utm[0]
 if utm[1]:session['utm_campaign']=utm[1]
 if role=='admin':return redirect('/admin')
 if isinstance(pending_fav,int) and pending_fav>0:
  with db() as conn:
   visible=conn.execute("""SELECT 1 FROM products WHERE id=?
       AND (active=1 OR (source='safka' AND supplier_listed=1))""",(pending_fav,)).fetchone()
   if visible:
    conn.execute("""INSERT INTO favorites(user_id,product_id) VALUES(?,?)
       ON CONFLICT(user_id,product_id) DO NOTHING""",(uid,pending_fav))
 if continue_checkout and previous_cart:
  return redirect('/checkout')
 if pending_fav or guest_favs:
  return redirect('/favorites')
 return redirect('/account')


@app.route('/register',methods=['GET','POST'])
def register():
 if request.method=='POST':
  email=request.form.get('email','').strip().lower();pw=request.form.get('password','')
  if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(pw)<10:flash('بريد صحيح وكلمة مرور 10 أحرف على الأقل')
  else:
   try:
    with db() as c:
     cur=c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',(email,generate_password_hash(pw),'customer'))
     new_user_id=cur.lastrowid
    return complete_customer_auth(new_user_id,'customer')
   except Exception as exc:
    if isinstance(exc, sqlite3.IntegrityError) or (exc.__class__.__name__ == 'UniqueViolation' and exc.__class__.__module__.startswith('psycopg')):flash('البريد مستخدم بالفعل')
    else:raise
 return render_template('auth.html',mode='register',social_providers=configured_providers())
@app.route('/admin/setup',methods=['GET','POST'])
def setup_first_admin():
 # Disabled unless the owner explicitly configures a private one-time setup token.
 token=os.getenv('SHOPY_ADMIN_SETUP_TOKEN','')
 if len(token)<24:abort(404)
 with db() as c:
  existing=c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
 if existing:abort(404)
 if request.method=='POST':
  submitted=request.form.get('setup_token','')
  email=request.form.get('email','').strip().lower()
  password=request.form.get('password','')
  if not hmac.compare_digest(token,submitted):
   abort(403)
  if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email) or len(password)<16:
   flash('اكتب بريدًا صحيحًا وكلمة مرور لا تقل عن 16 حرفًا')
  else:
   # A second check avoids accidentally creating another owner on a retry.
   with db() as c:
    if c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone():abort(409)
    c.execute("INSERT INTO users(email,password,role) VALUES(?,?,?)",(email,generate_password_hash(password),'admin'))
   return redirect('/login')
 # Avoid embedding the token in a URL, logs, or HTML.
 from markupsafe import escape
 return app.response_class("""<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="robots" content="noindex"><title>إنشاء الأدمن الأول</title><body><h2>إنشاء الأدمن الأول (مرة واحدة فقط)</h2><form method="post"><input type="hidden" name="csrf" value="%s"><label>البريد الإلكتروني <input type="email" name="email" required autocomplete="email"></label><br><label>كلمة المرور الجديدة <input type="password" name="password" minlength="16" required autocomplete="new-password"></label><br><label>رمز الإعداد الخاص <input type="password" name="setup_token" required autocomplete="off"></label><br><button type="submit">إنشاء حساب الأدمن</button></form></body></html>""" % escape(csrf()),mimetype='text/html')

@app.get('/privacy')
def privacy_policy():
 """Public, stable privacy URL required by Meta and customers."""
 return render_template('privacy_policy.html')

@app.get('/data-deletion')
def data_deletion_instructions():
 """Public instructions for the existing confirmed customer account removal flow."""
 return render_template('data_deletion.html')

@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  with db() as c:u=c.execute('SELECT * FROM users WHERE email=?',(request.form.get('email','').strip().lower(),)).fetchone()
  if u and check_password_hash(u['password'],request.form.get('password','')):
   return complete_customer_auth(u['id'],u['role'])
  flash('بيانات الدخول غير صحيحة')
 return render_template('auth.html',mode='login',social_providers=configured_providers())
@app.post('/auth/<provider>/start')
def social_start(provider):
 if provider not in ('google','facebook'):abort(404)
 u=current_user()
 if u and u['role']=='admin':
  with db() as c:owner=c.execute('SELECT password FROM users WHERE id=?',(u['id'],)).fetchone()
  if not check_password_hash(owner['password'],request.form.get('password','')):
   flash('لتأمين حساب الإدارة، أكد كلمة المرور قبل ربط حساب خارجي.')
   return redirect('/account')
 attempts=session.get('oauth_attempts',[])
 attempts=[t for t in attempts if isinstance(t,(int,float)) and 0<=time.time()-t<600]
 if len(attempts)>=10:abort(429)
 session['oauth_attempts']=attempts+[time.time()]
 try:
  flow,url=start_flow(provider,link_uid=u['id'] if u else None)
 except SocialAuthError:
  flash('التسجيل بالطريقة دي غير متاح حاليًا. تقدر تستخدم البريد وكلمة المرور.')
  return redirect('/account' if u else '/login')
 session['oauth_flow']=flow
 return redirect(url)

@app.get('/auth/callback')
def social_callback():
 flow=session.get('oauth_flow')
 supplied=request.args.get('flow','')
 if (not isinstance(flow,dict) or not isinstance(flow.get('state'),str)
     or not hmac.compare_digest(supplied,flow['state'])
     or not 0<=time.time()-flow.get('created',0)<=600):
  flash('انتهت صلاحية محاولة الدخول. ابدأ تسجيل الدخول مرة أخرى.')
  return redirect('/login')
 session.pop('oauth_flow',None)  # One use, also on provider denial or failure.
 if request.args.get('error'):
  flash('لم يكتمل تسجيل الدخول. تقدر تعيد المحاولة.')
  return redirect('/login')
 if flow.get('link_uid') is not None and session.get('uid')!=flow['link_uid']:
  abort(400)
 try:
  identity=exchange_identity(flow,request.args.get('code',''))
  with db() as c:
   u=resolve_account(c,identity,link_uid=flow.get('link_uid'),password_hash=generate_password_hash)
 except SocialAuthError as exc:
  flash(str(exc) if str(exc).startswith(('البريد','الحساب','لديك','ادخل')) else 'تعذر تأكيد تسجيل الدخول. حاول مرة أخرى.')
  return redirect('/login')
 except Exception as exc:
  if isinstance(exc,sqlite3.IntegrityError) or exc.__class__.__name__=='UniqueViolation':
   flash('الحساب مربوط بالفعل أو حدثت محاولة متزامنة. حاول تسجيل الدخول مجددًا.')
   return redirect('/login')
  raise
 return complete_customer_auth(u['id'],u['role'])

@app.post('/logout')
def logout():session.clear();return redirect('/')
@app.route('/account')
def account():
 u=current_user()
 if not u:return redirect('/login')
 with db() as c:
  orders,pager=paginate(c,'SELECT * FROM orders WHERE user_id=?','SELECT COUNT(*) FROM orders WHERE user_id=?',[u['id']],'id DESC',request.args.get('page'),20)
  ids=[o['id'] for o in orders]
  details=grouped(c,'order_items','order_id',ids,order='id')
  supplier_states={oid:rows[0] for oid,rows in grouped(c,'safka_order_outbox','order_id',ids).items() if rows}
 return render_template('account.html',orders=orders,details=details,supplier_states=supplier_states,pager=pager)

@app.post('/account/delete')
def delete_customer_account():
 """Delete only the authenticated storefront customer, never the store owner.

 A deliberate second confirmation and recent authentication protect against a
 stale/shared browser session. Preserve order/financial records but unlink them
 from the deleted login; linked social identities and favorites cascade away.
 """
 u=current_user()
 if not u:return redirect('/login')
 if u['role']!='customer':abort(403)
 if not 0 <= time.time()-session.get('recent_auth_at',0) <= 900:
  flash('لحماية حسابك، سجّل خروج وادخل مرة تانية قبل تأكيد الحذف.')
  return redirect('/account')
 if request.form.get('confirm_email','').strip().lower()!=u['email'].lower() or request.form.get('confirm_word','').strip()!='حذف':
  flash('لم يتم الحذف: اكتب بريد الحساب وكلمة حذف بالشكل المطلوب.')
  return redirect('/account')
 with db() as c:
  # Orders, totals and supplier references are operational/financial records.
  # Unlink the account while preserving fulfillment history for the merchant.
  c.execute('UPDATE orders SET user_id=NULL WHERE user_id=?',(u['id'],))
  c.execute("DELETE FROM users WHERE id=? AND role='customer'",(u['id'],))
 session.clear()
 flash('تم حذف حساب المتجر. تُحفظ سجلات الطلبات اللازمة للتوصيل والمحاسبة.')
 return redirect('/')

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


@app.template_filter('egypt_promo_date')
def egypt_promo_date(value):
 try:return datetime.fromtimestamp(int(value),tz=ZoneInfo('Africa/Cairo')).strftime('%Y-%m-%dT%H:%M')
 except (ValueError,TypeError,OverflowError):return ''


@app.get('/admin/promo-codes')
@admin_only
def admin_promo_codes():
 with db() as conn:codes=conn.execute('SELECT * FROM promo_codes ORDER BY created_at DESC,code LIMIT 200').fetchall()
 return render_template('promo_admin.html',codes=codes,now_epoch=int(time.time()),kind_labels=KIND_LABELS)


def egypt_datetime_epoch(value):
 if not value:raise PromoError('حدد تاريخ ووقت بداية ونهاية الكود.')
 try:
  moment=datetime.strptime(value,'%Y-%m-%dT%H:%M')
  return int(moment.replace(tzinfo=ZoneInfo('Africa/Cairo')).timestamp())
 except (ValueError,OverflowError):
  raise PromoError('صيغة التاريخ والوقت غير صحيحة.') from None


def read_promo_admin_settings():
 """Server-enforced form rules; percentage and monetary amounts never mixed."""
 kind=request.form.get('kind','percent').strip()
 percent=int(request.form.get('percent','0') or 0) if kind=='percent' else 0
 amount=(parse_money(request.form.get('amount_egp','')) if kind in ('fixed_order','fixed_item','shipping_amount') else 0)
 minimum=int(request.form.get('min_items','1') or 1)
 maximum=(int(request.form.get('max_discounted_items','')) if kind=='fixed_item' and
          request.form.get('max_discounted_items','').strip() else None)
 max_uses=None if request.form.get('unlimited')=='1' else int(request.form.get('max_uses',''))
 starts=egypt_datetime_epoch(request.form.get('starts_at',''))
 ends=egypt_datetime_epoch(request.form.get('expires_at',''))
 validate_settings(kind,percent,amount,minimum,maximum,max_uses,starts,ends)
 return kind,percent,amount,minimum,maximum,max_uses,starts,ends


def promo_admin_response(message,*,error=False):
 if 'application/json' in request.headers.get('Accept',''):
  return jsonify(ok=not error,message=message),(422 if error else 200)
 flash(message)
 return redirect('/admin/promo-codes')


@app.post('/admin/promo-codes/create')
@admin_only
def admin_create_promo():
 try:
  kind,percent,amount,minimum,maximum,uses,start,end=read_promo_admin_settings()
  with db() as conn:
   create_code(conn,request.form.get('code',''),percent,uses,start,end,
               kind=kind,amount_piastres=amount,min_items=minimum,max_discounted_items=maximum)
  return promo_admin_response('تم إنشاء كود الخصم بنجاح.')
 except (PromoError,ValueError) as exc:
  return promo_admin_response(str(exc) if isinstance(exc,PromoError) else 'راجع نوع الخصم والمبلغ وعدد القطع والتواريخ.',error=True)


@app.post('/admin/promo-codes/<code>/toggle')
@admin_only
def admin_toggle_promo(code):
 try:code=normalize_code(code)
 except PromoError:abort(400)
 with db() as conn:
  row=conn.execute('SELECT active FROM promo_codes WHERE code=?',(code,)).fetchone()
  if row is None:abort(404)
  conn.execute('UPDATE promo_codes SET active=? WHERE code=?',(0 if row['active'] else 1,code))
 return promo_admin_response('تم تحديث حالة كود الخصم.')


@app.post('/admin/promo-codes/<code>/edit')
@admin_only
def admin_edit_promo(code):
 try:
  code=normalize_code(code)
  kind,percent,amount,minimum,maximum,limit,starts,expires=read_promo_admin_settings()
  with db() as conn:
   row=conn.execute('SELECT used_count FROM promo_codes WHERE code=?',(code,)).fetchone()
   if not row:abort(404)
   if limit is not None and limit<row['used_count']:
    raise PromoError('الحد الجديد ماينفعش يكون أقل من عدد الاستخدامات السابقة.')
   conn.execute("""UPDATE promo_codes SET kind=?,percent=?,amount_piastres=?,min_items=?,
                  max_discounted_items=?,max_uses=?,starts_at=?,expires_at=? WHERE code=?""",
                (kind,percent,amount,minimum,maximum,limit,starts,expires,code))
  return promo_admin_response('تم حفظ تعديل الكود بنجاح.')
 except (PromoError,ValueError) as exc:
  return promo_admin_response(str(exc) if isinstance(exc,PromoError) else 'راجع القيم المدخلة.',error=True)


@app.get('/admin/safka-orders')
@admin_only
def admin_safka_orders_board():
 with db() as c:
  rows=c.execute("""SELECT o.reference,o.created_at,o.status AS local_status,
        COALESCE(s.status,'not_queued') AS push_status,
        COALESCE(s.supplier_status,'') AS supplier_status,
        s.updated_at,s.attempt_count
      FROM orders o LEFT JOIN safka_order_outbox s ON s.order_id=o.id
      ORDER BY o.id DESC LIMIT 100""").fetchall()
 attention=sum(1 for r in rows if r['push_status'] in ('unknown','preflight_failed','failed') and r['supplier_status'] not in ('collected','available'))
 return render_template('safka_orders_admin.html',orders=rows,attention=attention,supplier_labels=SUPPLIER_STATUS_LABELS)


@app.get('/admin/profit')
@admin_only
def admin_profit_report():
 # Recognition: a supplier-collected order is not the same as an order created
 # or marked delivered. All numbers below are in piastres, not pounds.
 with db() as c:
  r=c.execute("""WITH per_order AS (
    SELECT o.id,o.status,o.total,o.shipping_supplier_cost,
           COALESCE(s.supplier_status,'') AS supplier_status,
           COUNT(oi.id) AS line_count,
           COALESCE(SUM(CASE WHEN oi.unit_cost IS NULL THEN 1 ELSE 0 END),0) AS missing_items,
           COALESCE(SUM(COALESCE(oi.unit_cost,0)*oi.quantity),0) AS item_cost
    FROM orders o
    LEFT JOIN order_items oi ON oi.order_id=o.id
    LEFT JOIN safka_order_outbox s ON s.order_id=o.id
    GROUP BY o.id,o.status,o.total,o.shipping_supplier_cost,s.supplier_status
  )
  SELECT
    COUNT(*) AS orders_total,
    COALESCE(SUM(CASE WHEN status='cancelled' OR supplier_status IN ('declined1','declined2','returned1','returned2','returned_exchange','skip') THEN 1 ELSE 0 END),0) AS cancelled_count,
    COALESCE(SUM(CASE WHEN supplier_status='collected' AND status!='cancelled' THEN total ELSE 0 END),0) AS collected_value,
    COALESCE(SUM(CASE WHEN supplier_status='collected' AND status!='cancelled' AND shipping_supplier_cost IS NOT NULL AND missing_items=0 AND line_count>0 THEN total-item_cost-shipping_supplier_cost ELSE 0 END),0) AS collected_margin_known,
    COALESCE(SUM(CASE WHEN supplier_status='collected' AND status!='cancelled' AND (shipping_supplier_cost IS NULL OR missing_items>0 OR line_count=0) THEN 1 ELSE 0 END),0) AS collected_missing_costs,
    COALESCE(SUM(CASE WHEN supplier_status!='collected' AND status!='cancelled' AND supplier_status NOT IN ('declined1','declined2','returned1','returned2','returned_exchange','skip') AND shipping_supplier_cost IS NOT NULL AND missing_items=0 AND line_count>0 THEN total-item_cost-shipping_supplier_cost ELSE 0 END),0) AS pending_margin_known,
    COALESCE(SUM(CASE WHEN supplier_status!='collected' AND status!='cancelled' AND supplier_status NOT IN ('declined1','declined2','returned1','returned2','returned_exchange','skip') AND (shipping_supplier_cost IS NULL OR missing_items>0 OR line_count=0) THEN 1 ELSE 0 END),0) AS pending_missing_costs
  FROM per_order""").fetchone()
  stages=c.execute("""SELECT COALESCE(NULLIF(s.supplier_status,''),'لم يصل تحديث المورد') AS supplier_status,
    COUNT(*) AS orders,COALESCE(SUM(o.total),0) AS value
    FROM orders o LEFT JOIN safka_order_outbox s ON s.order_id=o.id
    GROUP BY COALESCE(NULLIF(s.supplier_status,''),'لم يصل تحديث المورد')
    ORDER BY orders DESC""").fetchall()
 return render_template('profit_admin.html',report=r,stages=stages,supplier_labels=SUPPLIER_STATUS_LABELS)


@app.route('/admin')
@admin_only
def admin():
 try:where,args,order=order_filters(request.args)
 except ValueError as exc:flash(str(exc));where,args,order='',[],'o.id ASC'
 with db() as c:
  orders,orders_pager=paginate(c,'SELECT o.* FROM orders o'+where,'SELECT COUNT(*) FROM orders o'+where,args,order,request.args.get('orders_page'),20,'orders_page','orders')
  product_terms=[];product_args=[]
  product_q=request.args.get('product_q','').strip()[:80]
  if product_q:product_terms.append('(LOWER(title) LIKE LOWER(?) OR LOWER(slug) LIKE LOWER(?))');product_args.extend(['%'+product_q+'%']*2)
  product_category=request.args.get('product_category','').strip()[:200]
  if product_category:product_terms.append('category=?');product_args.append(product_category)
  product_where=' WHERE '+' AND '.join(product_terms) if product_terms else ''
  products,products_pager=paginate(c,'SELECT * FROM products'+product_where,'SELECT COUNT(*) FROM products'+product_where,product_args,'id DESC',request.args.get('products_page'),30,'products_page','products')
  ids=[o['id'] for o in orders];pids=[p['id'] for p in products]
  details=grouped(c,'order_items','order_id',ids,order='id')
  notify_status=grouped(c,'notification_outbox','order_ref',[o['reference'] for o in orders],order='id')
  product_media=grouped(c,'product_media','product_id',pids,order='position,id')
  media_counts={pid:len(product_media[pid]) for pid in pids}
  safka_states={oid:rows[0] for oid,rows in grouped(c,'safka_order_outbox','order_id',ids).items() if rows}
  rates=c.execute('SELECT * FROM shipping ORDER BY governorate').fetchall()
  zones=c.execute('SELECT * FROM shipping_zones ORDER BY governorate,city,area').fetchall()
  funnels=c.execute("SELECT COALESCE(NULLIF(utm_source,''),'مباشر / غير معروف') AS source,COALESCE(NULLIF(utm_campaign,''),'بدون حملة') AS campaign,COUNT(*) AS count,SUM(total) AS order_value FROM orders GROUP BY source,campaign ORDER BY count DESC LIMIT 12").fetchall()
  states=c.execute('SELECT status,COUNT(*) AS count FROM orders GROUP BY status ORDER BY count DESC').fetchall()
  margin=c.execute("SELECT COUNT(*) AS count,COALESCE(SUM(total-item_cost-shipping_supplier_cost),0) AS margin FROM (SELECT o.id,o.total,o.shipping_supplier_cost,SUM(CASE WHEN oi.unit_cost IS NULL THEN 1 ELSE 0 END) AS missing_cost,SUM(COALESCE(oi.unit_cost,0)*oi.quantity) AS item_cost FROM orders o LEFT JOIN order_items oi ON oi.order_id=o.id GROUP BY o.id,o.total,o.shipping_supplier_cost) AS costs WHERE shipping_supplier_cost IS NOT NULL AND missing_cost=0").fetchone()
  metrics=c.execute("SELECT COUNT(*) AS orders,COALESCE(SUM(total),0) AS value,SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) AS new_orders FROM orders").fetchone()
  active_products=c.execute('SELECT COUNT(*) FROM products WHERE active=1').fetchone()[0]
  order_governorates=[r['governorate'] for r in c.execute('SELECT DISTINCT governorate FROM orders ORDER BY governorate')]
  product_categories=[r['category'] for r in c.execute("SELECT DISTINCT category FROM products WHERE category!='' ORDER BY category")]
  settings={r['key']:r['value'] for r in c.execute('SELECT key,value FROM store_settings')};g.store_settings=settings
  safka_sync=c.execute('SELECT kind,updated,hidden,created_at,source_label FROM safka_sync_runs ORDER BY id DESC LIMIT 6').fetchall()
  safka_count=c.execute("SELECT COUNT(*) FROM products WHERE source='safka'").fetchone()[0]
  cleanup_state=safka_cleanup_summary(c)
 from safka_webhooks import webhook_token
 hook_base=request.url_root.rstrip('/')+'/integrations/safka/hook/'
 safka_hook_urls={kind:hook_base+kind+'/'+webhook_token(app.secret_key,kind) for kind in ('product','order','callback')}
 return render_template('admin.html',safka_hook_urls=safka_hook_urls,orders=orders,products=products,rates=rates,details=details,media_counts=media_counts,product_media=product_media,notify_status=notify_status,zones=zones,funnels=funnels,states=states,estimated_margin=margin['margin'],margin_order_count=margin['count'],settings=settings,wa_configured=os.environ.get('SHOPY_WA_ENABLED')=='1',safka_states=safka_states,safka_sync=safka_sync,safka_count=safka_count,cleanup_state=cleanup_state,safka_only=settings.get('catalog_mode')=='safka_only',cleanup_text=CONFIRM_TEXT,api_read_enabled=os.getenv('SHOPY_SAFKA_READ_ENABLED')=='1',api_key_present=bool(os.getenv('SHOPY_SAFKA_API_KEY')),orders_pager=orders_pager,products_pager=products_pager,metrics=metrics,active_products=active_products,order_governorates=order_governorates,product_categories=product_categories)

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
 title=request.form.get('title','').strip()[:180]
 desc=clean_description(request.form.get('description','').strip()[:20000])
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
  if product['source']=='safka':
   # Save a durable merchant-owned overlay. Supplier sync updates base data,
   # then reapplies these fields without changing verified supplier costs/stock.
   c.execute("""INSERT INTO product_overrides(product_id,title,description,category,price,options_json)
   VALUES(?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET title=excluded.title,
   description=excluded.description,category=excluded.category,price=excluded.price,
   options_json=excluded.options_json""",(pid,title,desc,category,price,json.dumps(options,ensure_ascii=False)))
   c.execute("UPDATE market_pricing SET status='manual_override' WHERE product_id=?",(pid,))
   c.execute('UPDATE products SET title=?,description=?,price=?,options_json=?,category=? WHERE id=?',
             (title,desc,price,json.dumps(options,ensure_ascii=False),category,pid))
  else:
   if safka_only_mode():abort(403)
   c.execute('UPDATE products SET title=?,description=?,price=?,options_json=?,category=?,supplier_cost=? WHERE id=?',
             (title,desc,price,json.dumps(options,ensure_ascii=False),category,supplier_cost,pid))
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
 target=request.form.get('return_to','')
 return redirect(target+'#orders' if target.startswith('/admin?') and '\\' not in target else '/admin#orders')
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

@app.post('/favorites/toggle/<int:pid>')
def toggle_favorite(pid):
 user=current_user();back=safe_back_url()
 with db() as conn:
  p=conn.execute("SELECT id FROM products WHERE id=? AND (active=1 OR (source='safka' AND supplier_listed=1))",(pid,)).fetchone()
  if not p:abort(404)
  ids=customer_favorite_ids(conn,user);added=pid not in ids
  if user:
   if not added:conn.execute('DELETE FROM favorites WHERE user_id=? AND product_id=?',(user['id'],pid))
   else:conn.execute('INSERT INTO favorites(user_id,product_id) VALUES(?,?) ON CONFLICT(user_id,product_id) DO NOTHING',(user['id'],pid))
  if added:ids.add(pid)
  else:ids.discard(pid)
  if not user:
   if len(ids)>200:return action_response('سجل دخولك لحفظ قائمة أكبر من المفضلة.',False,back,422)
   session['guest_favorites']=sorted(ids)
 return action_response('اتضاف للمفضلة' if added else 'اتشال من المفضلة',redirect_to=back,
                        favorite=added,favorite_count=len(ids),view_url='/favorites')

@app.get('/favorites')
def favorites():
 user=current_user()
 with db() as conn:
  fav_ids=customer_favorite_ids(conn,user)
  if user:
   where=" FROM products p JOIN favorites f ON f.product_id=p.id WHERE f.user_id=? AND (p.active=1 OR (p.source='safka' AND p.supplier_listed=1))";params=[user['id']];order='f.created_at DESC,p.id DESC'
  else:
   where=' FROM products p WHERE p.id IN ('+','.join('?' for _ in fav_ids)+") AND (p.active=1 OR (p.source='safka' AND p.supplier_listed=1))" if fav_ids else ' FROM products p WHERE 1=0'
   params=sorted(fav_ids);order='p.id DESC'
  items,pager=paginate(conn,'SELECT p.*'+where,'SELECT COUNT(*)'+where,params,order,request.args.get('page'),30)
  ratings_map=customer_product_rating_map(conn,[p['id'] for p in items])
 return render_template('favorites.html',items=items,fav_ids=fav_ids,ratings_map=ratings_map,pager=pager)

@app.post('/products/<int:pid>/review')
def submit_product_review(pid):
 user=current_user()
 if not user:
  flash('سجل دخولك علشان تقيّم المنتج.')
  return redirect('/login')
 raw=request.form.get('rating','')
 comment=request.form.get('comment','').strip()
 if raw not in ('1','2','3','4','5'):
  flash('اختار تقييم من نجمة إلى خمس نجوم.')
  return redirect('/products/'+request.form.get('slug',''))
 rating=int(raw)
 if rating<=3 and len(comment)<10:
  flash('شاركنا تجربتك في تعليق من 10 حروف على الأقل.')
  return redirect('/products/'+request.form.get('slug',''))
 if len(comment)>1200:
  flash('التعليق طويل جدًا؛ الحد الأقصى 1200 حرف.')
  return redirect('/products/'+request.form.get('slug',''))
 with db() as conn:
  p=conn.execute("""SELECT id,slug FROM products WHERE id=? AND
    (active=1 OR (source='safka' AND supplier_listed=1))""",(pid,)).fetchone()
  if not p:abort(404)
  # One editable review per signed-in customer per product.
  conn.execute("""INSERT INTO product_reviews(product_id,user_id,rating,comment)
     VALUES(?,?,?,?)
     ON CONFLICT(product_id,user_id) DO UPDATE SET rating=excluded.rating,
     comment=excluded.comment,updated_at=CURRENT_TIMESTAMP""",
     (pid,user['id'],rating,comment))
 flash('شكرًا على رأيك! اتسجل تقييمك للمنتج.')
 return redirect('/products/'+p['slug']+'#reviews')

# Old comparison links redirect to the new favorites experience; no compare UI.
@app.get('/compare')
def legacy_compare():
 return redirect('/favorites',code=302)


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
  if budget is not None and displayed_product_price(p)>budget:continue
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
  allowed=[p for p in products if (budget is None or displayed_product_price(p)<=budget) and (not category or p['category']==category)]
  refined=local_ai_recommend(allowed,query,budget,category)
  if refined is not None:matches=refined;engine='ollama'
 with db() as conn:
  fav_ids=customer_favorite_ids(conn,current_user())
  ratings_map=customer_product_rating_map(conn)
 return render_template('assistant.html',matches=matches,need=query,budget=raw_budget,
    category=category,categories=categories,warning=warning,
    asked=request.method=='POST',engine=engine,fav_ids=fav_ids,ratings_map=ratings_map)


@app.post('/integrations/safka/hook/<kind>/<token>')
def safka_incoming_hook(kind,token):
 from safka_webhooks import authenticate_hook,valid_callback_key
 if kind not in ('product','order','callback') or not authenticate_hook(app.secret_key,kind,token):
  abort(404)
 if request.content_length and request.content_length>32768:abort(413)
 if not request.is_json:abort(415)
 data=request.get_json(silent=True)
 if not isinstance(data,dict):abort(400)
 if kind=='callback':
  if not valid_callback_key(data,os.getenv('SHOPY_SAFKA_API_KEY','')):abort(403)
  return ('',204)
 # Safka productHook means the affiliate opened a product. It is NOT
 # verified inventory state and must never hide or publish our catalog.
 if kind=='order':
  # Token authenticated above; only the documented event and a known provider ID
  # can update supplier facts. Customer names/phones/amounts in the event are ignored.
  with db() as conn:apply_order_status_event(conn,data)
 return ('',202)


@app.get('/internal/safka/nightly')
def safka_nightly_sync():
 # Vercel Cron provides this header only when a CRON_SECRET is configured.
 expected=os.environ.get('CRON_SECRET','')
 offered=request.headers.get('Authorization','')
 if len(expected)<24 or not hmac.compare_digest(offered,'Bearer '+expected):
  abort(401)
 if os.environ.get('SHOPY_SAFKA_READ_ENABLED')!='1':
  return jsonify(ok=False,message='Supplier API reads not enabled'),503
 api_key=os.environ.get('SHOPY_SAFKA_API_KEY','').strip()
 if len(api_key)<12:
  return jsonify(ok=False,message='Supplier API is not configured'),503
 from safka_admin_sync import api_page,checked_products,sync_product_page,sync_shipping_snapshot
 from safka_checkout import shipping_snapshot
 started=time.monotonic()
 counts={'pages':0,'products':0,'shipping':0,'complete':False}
 try:
  with db() as conn:
   p=conn.execute("SELECT value FROM store_settings WHERE key='safka_admin_next_page'").fetchone()
   next_page=int(p['value']) if p else 1
  for _ in range(100):
   # Leave buffer for Vercel Function completion; continue unfinished scans tomorrow.
   if time.monotonic()-started>170:break
   envelope,records=api_page(api_key,'/api/v1/public/products','products',page=next_page,size=50,timeout=8)
   pages,size=checked_products(envelope,records,next_page)
   with db() as conn:result=sync_product_page(conn,records,next_page,pages)
   counts['pages']+=1
   counts['products']+=size
   if result['cycle_complete']:
    counts['complete']=True
    break
   next_page=result['next_page']
  if time.monotonic()-started<185:
   rates=shipping_snapshot(api_key)
   with db() as conn:counts['shipping']=sync_shipping_snapshot(conn,rates)['updated']
  return jsonify(ok=True,**counts)
 except (ValueError,OSError,TypeError,KeyError,CatalogValidationError) as exc:
  # Existing verified catalog and rates are retained; no customer-facing warning.
  return jsonify(ok=False,error_type=type(exc).__name__,**counts),502


@app.post('/admin/safka/sync')
@admin_only
def sync_safka_owner():
 # Owner-triggered importer. Each browser request processes exactly one page
 # with an atomic DB transaction and next-page checkpoint.
 wants_json='application/json' in request.headers.get('Accept','')
 def fail(message,status=422):
  if wants_json:return jsonify(ok=False,message=message),status
  flash(message)
  return redirect('/admin#integrations')
 if os.getenv('SHOPY_SAFKA_READ_ENABLED')!='1':
  return fail('قراءة API غير مفعلة')
 key=os.getenv('SHOPY_SAFKA_API_KEY','')
 if len(key.strip())<12:
  return fail('مفتاح API غير مهيأ')
 kind=request.form.get('kind','')
 if kind=='restart-products':
  # Owner starts a fresh full-catalog scan from page one.
  with db() as conn:
   for k,v in [('safka_admin_next_page','1'),('safka_cycle_generation',''),('safka_cycle_total_pages','')]:
    conn.execute("""INSERT INTO store_settings(key,value) VALUES(?,?)
         ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(k,v))
  result=dict(ok=True,kind=kind,message='بدأ تحديث الكتالوج الكامل من أول صفحة')
  if wants_json:return jsonify(result)
  flash(result['message'])
  return redirect('/admin#integrations')
 if kind not in ('products','shipping'):
  abort(400)
 from safka_admin_sync import api_page,checked_products,checked_shipping,sync_product_page,sync_shipping_snapshot
 from safka_bridge import CatalogValidationError
 from urllib.error import HTTPError,URLError
 path=os.getenv('SHOPY_SAFKA_PRODUCTS_PATH' if kind=='products' else 'SHOPY_SAFKA_SHIPPING_PATH','').strip()
 if not path:return fail('مسار صفقة الرسمي غير مضبوط')
 try:
  if kind=='products':
   with db() as conn:
    saved=conn.execute("SELECT value FROM store_settings WHERE key='safka_admin_next_page'").fetchone()
    page=int(saved['value']) if saved else 1
   payload,records=api_page(key,path,'products',page=page,size=50,timeout=8)
   pages,count=checked_products(payload,records,page)
   with db() as conn:result=sync_product_page(conn,records,page,pages)
   message='تم استيراد '+str(count)+' منتج. الدفعة '+str(page)+' من '+str(pages)+'.'
   outcome=dict(ok=True,kind=kind,imported=count,page=page,total_pages=pages,
                next_page=result['next_page'],cycle_complete=result['cycle_complete'],
                archived=result['archived'],message=message)
  else:
   payload,records=api_page(key,path,'shipping',page=1,size=50,timeout=8)
   rates=checked_shipping(payload,records)
   with db() as conn:result=sync_shipping_snapshot(conn,rates)
   message='تم حفظ '+str(result['updated'])+' مدينة بشحن صفقة + 10 جنيه.'
   outcome=dict(ok=True,kind=kind,imported=result['updated'],message=message,cycle_complete=True)
  if wants_json:return jsonify(outcome)
  flash(message)
 except HTTPError as exc:
  return fail('فشل الاتصال بصفقة (HTTP '+str(exc.code)+'). لم تُحفظ هذه الدفعة')
 except (URLError,OSError,ValueError,TypeError,KeyError,CatalogValidationError) as exc:
  return fail('توقفت المزامنة: '+type(exc).__name__+' — لا تغييرات جزئية على الدفعة')
 return redirect('/admin#integrations')


@app.post('/admin/safka/preview-import')
@admin_only
def preview_safka_import():
 # Preview only: do not use any write connection or invoke import_products.
 if os.getenv('SHOPY_SAFKA_READ_ENABLED') != '1':
  flash('معاينة صفقة: القراءة غير مفعلة.')
  return redirect('/admin')
 key=os.getenv('SHOPY_SAFKA_API_KEY','')
 if len(key.strip()) < 12:
  flash('معاينة صفقة: مفتاح API غير موجود.')
  return redirect('/admin')
 from safka_probe import preview_records
 from safka_bridge import normalize_product, normalize_shipping, expand_safka_shipping, CatalogValidationError
 from urllib.error import HTTPError, URLError
 for label,var,kind,normalizer in [
  ('المنتجات','SHOPY_SAFKA_PRODUCTS_PATH','products',normalize_product),
  ('الشحن','SHOPY_SAFKA_SHIPPING_PATH','shipping',normalize_shipping)]:
  path=os.getenv(var,'').strip()
  if not path:
   flash('معاينة صفقة - '+label+': مسار API غير مضبوط.')
   continue
  try:
   rows=preview_records(path,key,kind,timeout=8)
   if kind=='shipping':rows=expand_safka_shipping(rows)
   valid=0; blocked=0; missing_stock=0; categories_missing=0
   for raw in rows:
    try:
     mapped=normalizer(raw)
     valid+=1
     if kind=='products':
      if mapped['stock'] is None:missing_stock+=1
      if mapped['category']=='غير مصنف (صفقة)':categories_missing+=1
    except (CatalogValidationError,ValueError,TypeError,KeyError):
     blocked+=1
   flash('معاينة صفقة - '+label+': فُحصت '+str(len(rows))+' سجلات. صالحة للتحويل: '+str(valid)+'؛ تحتاج مراجعة: '+str(blocked)+
         ('؛ بلا مخزون مؤكد: '+str(missing_stock)+'؛ بلا تصنيف: '+str(categories_missing) if kind=='products' else '')+
         '. معاينة فقط — لم تُحفظ أي بيانات.')
  except HTTPError as exc:
   flash('معاينة صفقة - '+label+': رفض HTTP '+str(exc.code)+'.')
  except (URLError,OSError,ValueError,TypeError,CatalogValidationError) as exc:
   flash('معاينة صفقة - '+label+': تعذرت القراءة ('+type(exc).__name__+'). لم تُحفظ بيانات.')
 return redirect('/admin')

@app.post('/admin/safka/check-connection')
@admin_only
def check_safka_connection():
 # A voluntary read-only diagnostic, strictly for the signed-in owner.
 # No DB writes, import, webhook registration, or provider order submission.
 if os.getenv('SHOPY_SAFKA_READ_ENABLED') != '1':
  flash('صفقة: تفعيل قراءة API غير مفعل على الاستضافة.')
  return redirect('/admin')
 key=os.getenv('SHOPY_SAFKA_API_KEY','')
 if len(key.strip()) < 12:
  flash('صفقة: مفتاح API غير مُهيأ على الخادم.')
  return redirect('/admin')
 from safka_probe import safe_keys
 from urllib.error import HTTPError, URLError
 for label,variable in [('المنتجات','SHOPY_SAFKA_PRODUCTS_PATH'),('الشحن','SHOPY_SAFKA_SHIPPING_PATH')]:
  path=os.getenv(variable,'').strip()
  if not path:
   flash('صفقة - '+label+': مسار القراءة الرسمي لم يُضبط بعد ('+variable+').')
   continue
  try:
   data=safe_keys(path,key,timeout=8)
   # Show field names only; never expose response values, the API key, or raw exceptions.
   fields=[str(x)[:48] for x in data.get('record_field_names',[])[:20]]
   top=[str(x)[:48] for x in data.get('top_level_fields',[])[:15]]
   flash('صفقة - '+label+': نجح طلب القراءة. عدد السجلات في العينة: '+str(data['sample_record_count'])+'؛ الحقول: '+(', '.join(fields) or 'لم تتوفر حقول سجلات')+'؛ الغلاف: '+(', '.join(top) or 'لا يوجد')+'.')
  except HTTPError as exc:
   flash('صفقة - '+label+': رفض HTTP '+str(exc.code)+'. راجع الصلاحيات والمسار الرسمي.')
  except (URLError,TimeoutError,OSError,ValueError,TypeError) as exc:
   flash('صفقة - '+label+': فشل الفحص ('+type(exc).__name__+'). لم يتغير أي منتج.')
 return redirect('/admin')

@app.get('/admin/integrations')
@admin_only
def integrations_redirect():return redirect('/admin#integrations')

@app.get('/admin/pricing')
@admin_only
def pricing_admin():
 where="p.active=1 AND p.source='safka'";params=[]
 term=request.args.get('q','').strip()[:80];status=request.args.get('status','')
 if term:where+=' AND p.title LIKE ?';params.append('%'+term+'%')
 if status in ('verified','cost_based','needs_review','cost_changed','manual_override'):
  where+=" AND COALESCE(m.status,'needs_review')=?";params.append(status)
 with db() as c:
  source='FROM products p LEFT JOIN market_pricing m ON m.product_id=p.id WHERE '+where
  items,pager=paginate(c,"SELECT p.*,m.status AS review_status,m.competitor_price,m.source_url,m.checked_at,m.notes,m.evidence_json "+source,'SELECT count(*) '+source,params,'p.id DESC',request.args.get('page'),30)
  enabled=c.execute("SELECT value FROM store_settings WHERE key='margin_shipping_discount_enabled'").fetchone()
 data=[]
 for row in items:
  value=dict(row)
  try:value['sources']=json.loads(value.get('evidence_json') or '[]')
  except ValueError:value['sources']=[]
  data.append(value)
 return render_template('pricing_admin.html',items=data,pager=pager,discount_enabled=bool(enabled and enabled['value']=='1'))

@app.post('/admin/pricing/shipping-policy')
@admin_only
def pricing_shipping_policy():
 with db() as c:c.execute("INSERT INTO store_settings(key,value) VALUES('margin_shipping_discount_enabled',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",('1' if request.form.get('enabled')=='1' else '0',))
 flash('تم حفظ سياسة خصم الشحن.')
 return redirect('/admin/pricing')

@app.post('/admin/pricing/<int:pid>')
@admin_only
def approve_market_price(pid):
 from datetime import datetime,timezone
 from profit_pricing import minimum_sale_price
 from urllib.parse import urlsplit
 price=parse_money(request.form.get('price'));competitor=parse_money(request.form.get('competitor_price'))
 url=request.form.get('source_url','').strip()[:2000];parts=urlsplit(url)
 notes=request.form.get('notes','').strip()[:1500]
 if parts.scheme!='https' or not parts.hostname or parts.username or len(notes)<10 or competitor<=0:abort(400)
 with db() as c:
  p=c.execute("SELECT * FROM products WHERE id=? AND source='safka'",(pid,)).fetchone()
  if not p:abort(404)
  if not isinstance(p['supplier_cost'],int) or price<minimum_sale_price(p['supplier_cost']):
   flash('السعر أقل من حد الربح المتفق عليه أو التكلفة غير معروفة. راجع السعر.')
   return redirect('/admin/pricing')
  c.execute("""INSERT INTO market_pricing(product_id,target_price,verified_cost,competitor_price,source_url,checked_at,status,notes,previous_price)
  VALUES(?,?,?,?,?,?,'verified',?,?) ON CONFLICT(product_id) DO UPDATE SET target_price=excluded.target_price,verified_cost=excluded.verified_cost,competitor_price=excluded.competitor_price,source_url=excluded.source_url,checked_at=excluded.checked_at,status='verified',basis='market',notes=excluded.notes,previous_price=excluded.previous_price""",(pid,price,p['supplier_cost'],competitor,url,datetime.now(timezone.utc).isoformat(),notes,p['price']))
  c.execute('UPDATE products SET price=? WHERE id=?',(price,pid))
 flash('تم اعتماد السعر مع حمايته أثناء المزامنة.')
 return redirect('/admin/pricing')

@app.get('/admin/social-login')
@admin_only
def social_login_setup():
 from social_auth import config,auth_request
 configured=config();external={}
 if configured:
  try:external=auth_request('settings').get('external',{})
  except SocialAuthError:pass
 return render_template('social_setup.html',social_providers=configured_providers(),external=external,auth_configured=bool(configured))

@app.route('/robots.txt')
def robots():return app.response_class('User-agent: *\nDisallow: /admin\nDisallow: /account\nDisallow: /checkout\nDisallow: /cart\nDisallow: /favorites\nDisallow: /assistant\nDisallow: /order\n',mimetype='text/plain')
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
