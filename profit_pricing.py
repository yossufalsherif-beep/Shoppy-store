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
                           ('retained_profit_floor','BIGINT NOT NULL DEFAULT 0')]:
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


def validate_delivery_snapshot(raw,subtotal,costs):
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
