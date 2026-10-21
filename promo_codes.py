"""Owner-managed percentage promo codes with server-side limits and atomic redemption.

No supplier API operations. EGP totals remain integer piastres throughout.
Times are UTC Unix seconds; Egypt's local time is converted only at admin input.
"""
import os
import re
import time

CODE_FORMAT=re.compile(r'^[A-Z0-9](?:[A-Z0-9_-]{2,30}[A-Z0-9])$')
MIN_CONTRIBUTION=100  # Never create a merchant loss; require >= EGP 1.


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
        percent INTEGER NOT NULL CHECK(percent BETWEEN 1 AND 100),
        max_uses INTEGER CHECK(max_uses IS NULL OR max_uses > 0),
        used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
        starts_at BIGINT NOT NULL,
        expires_at BIGINT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
        created_at BIGINT NOT NULL,
        CHECK(expires_at > starts_at)
    )""")
    cols={row['name'] for row in conn.execute('PRAGMA table_info(orders)')}
    for name,ddl in (
        ('promo_code',"TEXT NOT NULL DEFAULT ''"),
        ('promo_percent','INTEGER NOT NULL DEFAULT 0'),
        ('promo_discount','BIGINT NOT NULL DEFAULT 0')):
        if name not in cols:
            conn.execute('ALTER TABLE orders ADD COLUMN '+name+' '+ddl)
    if os.getenv('DATABASE_URL'):
        conn.execute('ALTER TABLE promo_codes ENABLE ROW LEVEL SECURITY')


def create_code(conn, code, percent, max_uses, starts_at, expires_at, *, now=None):
    code=normalize_code(code)
    now=int(time.time()) if now is None else int(now)
    if type(percent) is not int or not 1 <= percent <= 100:
        raise PromoError('نسبة الخصم لازم تكون من 1% إلى 100%.')
    if max_uses is not None and (type(max_uses) is not int or not 1 <= max_uses <= 10000000):
        raise PromoError('عدد الاستخدامات لازم يكون عددًا موجبًا، أو اختار غير محدود.')
    if (type(starts_at) is not int or type(expires_at) is not int
        or expires_at <= starts_at or expires_at <= now):
        raise PromoError('حدد تاريخ بداية ونهاية صحيحة؛ النهاية لازم تكون في المستقبل.')
    result=conn.execute("""INSERT INTO promo_codes
        (code,percent,max_uses,starts_at,expires_at,created_at)
        VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO NOTHING""",
        (code,percent,max_uses,starts_at,expires_at,now))
    if result.rowcount != 1:
        raise PromoError('كود الخصم ده موجود بالفعل.')
    return code


def get_active_code(conn, code, *, now=None):
    code=normalize_code(code)
    now=int(time.time()) if now is None else int(now)
    row=conn.execute('SELECT * FROM promo_codes WHERE code=?',(code,)).fetchone()
    if row is None or not row['active']:
        raise PromoError('كود الخصم غير متاح.')
    if now < row['starts_at']:
        raise PromoError('كود الخصم لسه ما بدأش.')
    if now >= row['expires_at']:
        raise PromoError('صلاحية كود الخصم انتهت.')
    if row['max_uses'] is not None and row['used_count']>=row['max_uses']:
        raise PromoError('عدد استخدامات كود الخصم اكتمل.')
    return row


def calculate_discount(conn, code, offer, rows, rate, *, now=None):
    """Calculate after bundle discount and before adding shipping.

    Source of truth is supplier costs and shipping snapshot, not browser input.
    Failure preserves the checkout but refuses a loss-making code.
    """
    if not code:
        return 0,0
    promo=get_active_code(conn,code,now=now)
    base=offer['merchandise_total']
    amount=base*promo['percent']//100
    if amount<=0:
        raise PromoError('السلة مش مؤهلة لخصم بهذا الحجم.')
    if not rows or rate is None or not isinstance(rate[1],int):
        raise PromoError('لازم نحدد تكلفة توصيل صحيحة قبل استخدام الكود.')
    goods=0
    for p,qty,*_ in rows:
        cost=p['supplier_cost']
        if type(qty) is not int or qty<=0 or type(cost) is not int or cost<=0:
            raise PromoError('لازم نراجع تكلفة المنتجات قبل تطبيق الخصم.')
        goods+=qty*cost
    # Respect the existing order's guaranteed retained contribution.
    floor=max(MIN_CONTRIBUTION,offer.get('floor') or 0)
    if base-amount+offer['fee']-goods-rate[1]<floor:
        raise PromoError('الكود مش متاح على السلة دي لأن الخصم أكبر من هامش الربح المسموح.')
    return amount,promo['percent']


def consume_code(conn, code, *, now=None):
    """Atomic limited-use claim inside the already-open checkout transaction.

    Never call before checking the request idempotency key.
    """
    normalized=normalize_code(code)
    now=int(time.time()) if now is None else int(now)
    result=conn.execute("""UPDATE promo_codes SET used_count=used_count+1
         WHERE code=? AND active=1 AND starts_at<=? AND expires_at>?
           AND (max_uses IS NULL OR used_count<max_uses)""",
         (normalized,now,now))
    if result.rowcount!=1:
        raise PromoError('الكود انتهى أو استُخدم بالكامل؛ حدث ملخص الطلب.')
