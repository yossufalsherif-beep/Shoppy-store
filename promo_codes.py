"""Owner-managed promos: percent, fixed order/item, shipping and free-shipping.

Values are integer piastres; percent is an integer 1..100.
Every redemption is rechecked under the same database transaction as order insert.
"""
import os
import re
import time

CODE_FORMAT=re.compile(r'^[A-Z0-9](?:[A-Z0-9_-]{2,30}[A-Z0-9])$')
KINDS=('percent','fixed_order','fixed_item','shipping_amount','free_shipping')
MIN_CONTRIBUTION=100

KIND_LABELS={
 'percent':'نسبة خصم على المنتجات',
 'fixed_order':'مبلغ ثابت من المنتجات',
 'fixed_item':'مبلغ لكل قطعة',
 'shipping_amount':'مبلغ من الشحن',
 'free_shipping':'شحن مجاني',
}

class PromoError(ValueError):
    pass


def normalize_code(code):
    value=str(code or '').strip().upper()
    if not CODE_FORMAT.fullmatch(value):
        raise PromoError('كود الخصم لازم يكون 4–32 حرف إنجليزي أو رقم أو شرطة.')
    return value


def init_promo_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS promo_codes(
        code TEXT PRIMARY KEY,
        kind TEXT NOT NULL DEFAULT 'percent'
           CHECK(kind IN ('percent','fixed_order','fixed_item','shipping_amount','free_shipping')),
        percent INTEGER NOT NULL DEFAULT 0 CHECK(percent BETWEEN 0 AND 100),
        amount_piastres BIGINT NOT NULL DEFAULT 0 CHECK(amount_piastres>=0),
        min_items INTEGER NOT NULL DEFAULT 1 CHECK(min_items BETWEEN 1 AND 1000),
        max_discounted_items INTEGER CHECK(max_discounted_items IS NULL OR max_discounted_items BETWEEN 1 AND 1000),
        max_uses INTEGER CHECK(max_uses IS NULL OR max_uses>0),
        used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count>=0),
        starts_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_at BIGINT NOT NULL,
        CHECK(expires_at>starts_at)
    )""")
    # Safe forward-only columns if the feature is initialized on older local DBs.
    c={row['name'] for row in conn.execute('PRAGMA table_info(promo_codes)')}
    for name,definition in [
       ('kind',"TEXT NOT NULL DEFAULT 'percent'"),
       ('amount_piastres','BIGINT NOT NULL DEFAULT 0'),
       ('min_items','INTEGER NOT NULL DEFAULT 1'),
       ('max_discounted_items','INTEGER')]:
        if name not in c:conn.execute('ALTER TABLE promo_codes ADD COLUMN '+name+' '+definition)
    c={row['name'] for row in conn.execute('PRAGMA table_info(orders)')}
    for name,definition in [
       ('promo_code',"TEXT NOT NULL DEFAULT ''"),
       ('promo_percent','INTEGER NOT NULL DEFAULT 0'),
       ('promo_discount','BIGINT NOT NULL DEFAULT 0'),
       ('promo_shipping_discount','BIGINT NOT NULL DEFAULT 0'),
       ('promo_kind',"TEXT NOT NULL DEFAULT ''"),
       ('promo_value','BIGINT NOT NULL DEFAULT 0'),
       ('promo_min_items','INTEGER NOT NULL DEFAULT 0'),
       ('promo_max_items','INTEGER NOT NULL DEFAULT 0')]:
        if name not in c:conn.execute('ALTER TABLE orders ADD COLUMN '+name+' '+definition)
    if os.getenv('DATABASE_URL'):
        conn.execute('ALTER TABLE promo_codes ENABLE ROW LEVEL SECURITY')


def validate_settings(kind,percent,amount_piastres,min_items,max_discounted_items,max_uses,starts_at,expires_at,*,now=None):
    now=int(time.time()) if now is None else int(now)
    if kind not in KINDS:raise PromoError('نوع الخصم غير معروف.')
    if type(percent) is not int or not 0<=percent<=100:raise PromoError('النسبة لازم تكون من 1% إلى 100%.')
    if type(amount_piastres) is not int or amount_piastres<0 or amount_piastres>100000000:
        raise PromoError('قيمة الخصم غير صحيحة.')
    if kind=='percent' and not 1<=percent<=100:raise PromoError('حدد نسبة خصم من 1% إلى 100%.')
    if kind in ('fixed_order','fixed_item','shipping_amount') and amount_piastres<=0:
        raise PromoError('حدد مبلغ الخصم بالجنيه.')
    if type(min_items) is not int or not 1<=min_items<=1000:
        raise PromoError('الحد الأدنى لعدد القطع لازم يكون عددًا موجبًا.')
    if max_discounted_items is not None and (
        kind!='fixed_item' or type(max_discounted_items) is not int or not 1<=max_discounted_items<=1000):
        raise PromoError('تحديد عدد القطع المستفيدة متاح فقط لخصم كل قطعة.')
    if max_uses is not None and (type(max_uses) is not int or not 1<=max_uses<=10000000):
        raise PromoError('عدد الاستخدامات لازم يكون موجبًا أو غير محدود.')
    if (type(starts_at) is not int or type(expires_at) is not int
        or expires_at<=starts_at or expires_at<=now):
        raise PromoError('حدد تاريخ بداية ونهاية صحيحين؛ النهاية لازم تكون في المستقبل.')


def create_code(conn,code,percent,max_uses,starts_at,expires_at,*,now=None,
                kind='percent',amount_piastres=0,min_items=1,max_discounted_items=None):
    code=normalize_code(code)
    now=int(time.time()) if now is None else int(now)
    validate_settings(kind,percent,amount_piastres,min_items,max_discounted_items,max_uses,starts_at,expires_at,now=now)
    result=conn.execute("""INSERT INTO promo_codes
        (code,kind,percent,amount_piastres,min_items,max_discounted_items,max_uses,starts_at,expires_at,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code) DO NOTHING""",
        (code,kind,percent,amount_piastres,min_items,max_discounted_items,max_uses,starts_at,expires_at,now))
    if result.rowcount!=1:raise PromoError('كود الخصم ده موجود بالفعل.')
    return code


def get_active_code(conn,code,*,now=None):
    code=normalize_code(code)
    now=int(time.time()) if now is None else int(now)
    row=conn.execute('SELECT * FROM promo_codes WHERE code=?',(code,)).fetchone()
    if row is None or not row['active']:raise PromoError('كود الخصم غير متاح.')
    if now<row['starts_at']:raise PromoError('كود الخصم لسه ما بدأش.')
    if now>=row['expires_at']:raise PromoError('صلاحية كود الخصم انتهت.')
    if row['max_uses'] is not None and row['used_count']>=row['max_uses']:
        raise PromoError('عدد استخدامات كود الخصم اكتمل.')
    return row


def quote_promo(conn,code,offer,rows,rate,*,now=None):
    """Return exact discounts for products and delivery without exceeding costs.

    Applies after the merchant bundle offer, never discounts already-free shipping.
    """
    empty={'code':'','kind':'','percent':0,'value':0,'min_items':0,'max_items':0,
           'product_discount':0,'shipping_discount':0}
    if not code:return empty
    promo=get_active_code(conn,code,now=now)
    if not rows or rate is None or type(rate[1]) is not int or rate[1]<0:
        raise PromoError('حدد عنوان شحن وتكلفته قبل استخدام الكود.')
    units=0
    goods=0
    for p,quantity,*_ in rows:
        cost=p['supplier_cost']
        if type(quantity) is not int or quantity<1 or type(cost) is not int or cost<=0:
            raise PromoError('لازم نراجع تكلفة المنتجات قبل تطبيق الخصم.')
        units+=quantity
        goods+=quantity*cost
    if units<promo['min_items']:
        raise PromoError('الكود متاح للطلبات التي تحتوي على '+str(promo['min_items'])+' قطعة على الأقل.')
    kind=promo['kind']
    base=offer['merchandise_total']
    delivery=offer['fee']
    value=promo['percent'] if kind=='percent' else promo['amount_piastres']
    max_items=promo['max_discounted_items'] or 0
    pdiscount=0
    sdiscount=0
    if kind=='percent':pdiscount=base*promo['percent']//100
    elif kind=='fixed_order':pdiscount=promo['amount_piastres']
    elif kind=='fixed_item':pdiscount=promo['amount_piastres']*min(units,max_items or units)
    elif kind=='shipping_amount':sdiscount=min(delivery,promo['amount_piastres'])
    elif kind=='free_shipping':sdiscount=delivery
    if (pdiscount>base or pdiscount<0 or (kind in ('shipping_amount','free_shipping') and sdiscount<=0)):
        raise PromoError('الخصم غير متاح على السلة دي؛ راجع الإجمالي أو الشحن المجاني الحالي.')
    if (pdiscount+sdiscount)<=0:
        raise PromoError('الخصم غير متاح على السلة دي.')
    floor=max(MIN_CONTRIBUTION,offer.get('floor') or 0)
    if base-pdiscount+delivery-sdiscount-goods-rate[1]<floor:
        raise PromoError('الكود مش متاح على السلة دي لأن الخصم أكبر من هامش الربح المسموح.')
    return {'code':promo['code'],'kind':kind,'percent':promo['percent'],
            'value':value,'min_items':promo['min_items'],'max_items':max_items,
            'product_discount':pdiscount,'shipping_discount':sdiscount}


def calculate_discount(conn,code,offer,rows,rate,*,now=None):
    """Backward-compatible helper for existing product-percentage tests."""
    quoted=quote_promo(conn,code,offer,rows,rate,now=now)
    return quoted['product_discount'],quoted['percent']


def consume_code(conn,code,*,now=None):
    normalized=normalize_code(code)
    now=int(time.time()) if now is None else int(now)
    # Atomic claim prevents redemption count oversubscription under concurrency.
    result=conn.execute("""UPDATE promo_codes SET used_count=used_count+1
         WHERE code=? AND active=1 AND starts_at<=? AND expires_at>?
           AND (max_uses IS NULL OR used_count<max_uses)""",
         (normalized,now,now))
    if result.rowcount!=1:
        raise PromoError('الكود انتهى أو استُخدم بالكامل؛ حدث ملخص الطلب.')
