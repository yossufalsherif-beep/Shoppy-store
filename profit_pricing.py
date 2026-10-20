"""Integer-only margin policy, market evidence and merchant-funded delivery."""
import os
from datetime import datetime, timezone

MIN_PROFIT=3000
MIN_MARGIN_BPS=1500
POLICY_VERSION='retained-margin-15pct-30egp-v1'


def retained_profit_floor(subtotal):
    return max(MIN_PROFIT,(subtotal*MIN_MARGIN_BPS+9999)//10000)


def minimum_sale_price(cost):
    """A 15% gross margin is cost/0.85, not cost*1.15. Round up to EGP 5."""
    floor=max(cost+MIN_PROFIT,(cost*10000+8499)//8500)
    return ((floor+499)//500)*500


def init_pricing_schema(conn):
    columns={r['name'] for r in conn.execute('PRAGMA table_info(orders)')}
    for col,definition in [('shipping_standard_fee','BIGINT'),('shipping_discount','BIGINT NOT NULL DEFAULT 0'),
                           ('retained_profit_floor','BIGINT NOT NULL DEFAULT 0'),('bundle_discount','BIGINT NOT NULL DEFAULT 0'),('bundle_offer_version','INTEGER NOT NULL DEFAULT 0'),('bundle_unit_count','INTEGER NOT NULL DEFAULT 0')]:
        if col not in columns:conn.execute('ALTER TABLE orders ADD COLUMN '+col+' '+definition)
    init_market_schema(conn)


def init_market_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS market_pricing(
        product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
        target_price BIGINT,verified_cost BIGINT,competitor_price BIGINT,
        source_url TEXT NOT NULL DEFAULT '',checked_at TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'needs_review',basis TEXT NOT NULL DEFAULT 'market',notes TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '[]',previous_price BIGINT,
        CHECK(target_price IS NULL OR target_price>=0))''')
    if 'basis' not in {r['name'] for r in conn.execute('PRAGMA table_info(market_pricing)')}:
        conn.execute("ALTER TABLE market_pricing ADD COLUMN basis TEXT NOT NULL DEFAULT 'market'")
    if os.getenv('DATABASE_URL'):conn.execute('ALTER TABLE market_pricing ENABLE ROW LEVEL SECURITY')


def shipping_offer(conn,rows,rate):
    standard,supplier=rate
    result={'standard':standard,'fee':standard,'discount':0,'floor':0}
    flag=conn.execute("SELECT value FROM store_settings WHERE key='margin_shipping_discount_enabled'").fetchone()
    if not flag or flag['value']!='1' or not rows or not isinstance(supplier,int) or supplier<0:
        return result
    # Unknown costs, mixed suppliers or stale/unverified costs never fund a discount.
    subtotal=0;cost=0
    for row in rows:
        p,q=row[:2]
        unit_cost=p['supplier_cost']
        if p['source']!='safka' or not isinstance(unit_cost,int) or unit_cost<=0 or not isinstance(q,int) or q<1:
            return result
        subtotal+=p['price']*q;cost+=unit_cost*q
    floor=retained_profit_floor(subtotal)
    surplus=subtotal+standard-cost-supplier-floor
    discount=min(standard,max(0,surplus))
    # Keep offers in full EGP; never round up merchant-funded savings.
    discount=(discount//100)*100
    return {'standard':standard,'fee':standard-discount,'discount':discount,'floor':floor}


def preserve_market_price(conn,pid,cost):
    rule=conn.execute('SELECT target_price,status,basis FROM market_pricing WHERE product_id=?',(pid,)).fetchone()
    if rule and rule['status'] in ('verified','cost_based','cost_changed') and rule['target_price'] is not None:
        guarded=max(rule['target_price'],minimum_sale_price(cost))
        conn.execute('UPDATE products SET price=? WHERE id=?',(guarded,pid))
        conn.execute("UPDATE market_pricing SET status=CASE WHEN verified_cost=? THEN CASE WHEN basis='cost' THEN 'cost_based' ELSE 'verified' END ELSE 'cost_changed' END WHERE product_id=?",(cost,pid))
        return guarded
    return None



# Opt-in promotion: a displayed EGP 95 premium on each piece, and an EGP 95
# order-level discount on each piece after the first. Historical DB catalog
# prices remain untouched, so a Safka sync never adds the premium twice.
BUNDLE_PREMIUM_PIASTRES = 9500

def bundle_enabled():
    return os.getenv('SHOPY_BUNDLE_OFFER_ENABLED', '0') == '1'

def displayed_product_price(product):
    return product['price'] + (BUNDLE_PREMIUM_PIASTRES if bundle_enabled() else 0)

def checkout_offer(conn, rows, rate):
    """Compute a fully funded, server-side price snapshot without updating products.

    The single-item shipping reduction is merchant-funded and explicitly
    configured in EGP; the quote always uses the verified governorate rate.
    The merchant's retained contribution cannot dip below the configured floor.
    """
    if not bundle_enabled():
        legacy = shipping_offer(conn, rows, rate)
        displayed = sum(p['price'] * q for p, q, *_ in rows)
        return dict(legacy, displayed_subtotal=displayed,
                    merchandise_total=displayed, bundle_discount=0,
                    bundle_offer_version=0, bundle_unit_count=sum(q for _, q, *_ in rows))
    if rate is None:
        raise ValueError('Shipping destination is unavailable')
    standard, supplier_shipping = rate
    if not all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in (standard, supplier_shipping)):
        raise ValueError('Verified supplier shipping cost is required')
    if not rows or any(not isinstance(q, int) or isinstance(q, bool) or q < 1
                       or p['supplier_cost'] is None or p['supplier_cost'] <= 0
                       for p, q, *_ in rows):
        raise ValueError('Supplier costs are missing; bundle offer requires price review')
    units = sum(q for _, q, *_ in rows)
    displayed = sum(displayed_product_price(p) * q for p, q, *_ in rows)
    discount = BUNDLE_PREMIUM_PIASTRES * (units - 1)
    merchandise = displayed - discount
    if units >= 2:
        delivery = 0
    else:
        raw_reduction = os.getenv('SHOPY_SINGLE_ITEM_SHIPPING_REDUCTION_EGP', '0')
        try:
            reduction = int(raw_reduction)
        except (ValueError, TypeError):
            raise ValueError('Invalid merchant shipping subsidy') from None
        if not 0 <= reduction <= 500:
            raise ValueError('Invalid merchant shipping subsidy')
        delivery = min(standard, max(100, standard - reduction * 100))
    floor = retained_profit_floor(merchandise)
    supplier_goods = sum(p['supplier_cost'] * q for p, q, *_ in rows)
    if merchandise + delivery - supplier_goods - supplier_shipping < floor:
        raise ValueError('العرض غير متاح لهذه السلة مؤقتًا؛ يحتاج مراجعة هامش الربح قبل البيع.')
    return {
        'standard': standard, 'fee': delivery, 'discount': standard - delivery,
        'floor': floor, 'displayed_subtotal': displayed,
        'merchandise_total': merchandise, 'bundle_discount': discount,
        'bundle_offer_version': 1, 'bundle_unit_count': units,
    }

def validate_delivery_snapshot(raw,subtotal,costs,quantity=None):
    if raw.get('bundle_offer_version', 0) == 1:
        standard=raw['shipping_standard_fee']
        supplier=raw['supplier_shipping_piastres']
        buyer=raw['customer_shipping_piastres']
        discount=raw['shipping_discount']
        bundle_discount=raw['bundle_discount']
        units=raw['bundle_unit_count']
        floor=raw['retained_profit_floor']
        if not all(isinstance(v,int) and not isinstance(v,bool) for v in (standard,supplier,buyer,discount,bundle_discount,units,floor)):
            raise ValueError('Invalid promotional order snapshot')
        if units < 1 or quantity != units or standard != supplier+1000 or not 0 <= buyer <= standard or discount != standard-buyer:
            raise ValueError('Promotional shipping snapshots do not reconcile')
        if bundle_discount != BUNDLE_PREMIUM_PIASTRES*(units-1) or (units>=2 and buyer!=0) or (units==1 and buyer<100):
            raise ValueError('Promotional discount does not match purchased quantity')
        merchandise=subtotal-bundle_discount
        if merchandise < 0 or floor != retained_profit_floor(merchandise) or merchandise+buyer-costs-supplier<floor:
            raise ValueError('Promotional order violates retained merchant contribution')
        return
    if raw.get('bundle_offer_version', 0) not in (0,None):
        raise ValueError('Unknown promotion version')
    standard=raw.get('shipping_standard_fee')
    discount=raw.get('shipping_discount',0)
    floor=raw.get('retained_profit_floor',0)
    supplier=raw['supplier_shipping_piastres'];buyer=raw['customer_shipping_piastres']
    if not isinstance(supplier,int) or supplier<0:raise ValueError('Missing shipping cost')
    if standard is None:standard=buyer
    if not all(isinstance(x,int) and not isinstance(x,bool) for x in (standard,discount,floor,buyer)):
        raise ValueError('Invalid delivery snapshot')
    if standard!=supplier+1000 or not 0<=discount<=standard or buyer!=standard-discount:
        raise ValueError('Delivery discount does not match the saved rate')
    if discount and (floor!=retained_profit_floor(subtotal) or subtotal+buyer-costs-supplier<floor):
        raise ValueError('Delivery discount violates the retained margin')
