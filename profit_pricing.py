"""Integer-only margin policy, market evidence and merchant-funded delivery."""
import os
from special_offers import product_offer, _field as _product_field
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
# v2 promotion permits reduced contribution for expensive destinations, while
# blocking sales below goods + real supplier shipping. Historical v1 keeps its floor.
BUNDLE_OFFER_VERSION = 3
SINGLE_ITEM_SHIPPING_PIASTRES = 5000  # Fixed EGP 50 for any supported destination.
MIN_BUNDLE_CONTRIBUTION_PIASTRES = 100  # At least EGP 1 before ads, returns, and other overhead.

def bundle_enabled():
    return os.getenv('SHOPY_BUNDLE_OFFER_ENABLED', '0') == '1'

def displayed_product_price(product):
    offer = product_offer(product)
    if offer:
        # Fixed offer price; never carries the bundle premium.
        return offer['price']
    return product['price'] + (BUNDLE_PREMIUM_PIASTRES if bundle_enabled() else 0)


def product_in_bundle(product):
    """Fixed-offer products are outside the EGP 95 per-extra-piece offer."""
    return product_offer(product) is None


def product_free_shipping(product):
    offer = product_offer(product)
    return bool(offer and offer.get('free_shipping'))


def product_sale_badge(product):
    """Struck-through price and an honest percentage (rounded DOWN), or None."""
    offer = product_offer(product)
    if not offer:
        return None
    original = offer.get('compare_at')
    shown = displayed_product_price(product)
    if type(original) is not int or shown <= 0 or original <= shown:
        return None
    return {'compare_at': original, 'percent': (original - shown) * 100 // original}


def order_has_free_shipping(rows):
    """Rule: delivery is free for the WHOLE order if it contains any
    free-shipping offer product. Supplier delivery is then merchant-funded."""
    return any(product_free_shipping(p) for p, *_ in rows)


def _apply_free_shipping(offer, rows, rate):
    standard, supplier_shipping = rate
    goods = [_product_field(p, 'supplier_cost') for p, *_ in rows]
    costs_known = (type(supplier_shipping) is int and supplier_shipping >= 0
                   and all(type(cost) is int and cost > 0 for cost in goods))
    if not costs_known and any(_product_field(p, 'source') == 'safka' for p, *_ in rows):
        raise ValueError('الشحن المجاني يحتاج تكلفة مورد مؤكدة؛ راجع السلة لاحقًا.')
    if costs_known:
        supplier_goods = sum(p['supplier_cost'] * q for p, q, *_ in rows)
        if (offer['merchandise_total'] - supplier_goods - supplier_shipping
                < MIN_BUNDLE_CONTRIBUTION_PIASTRES):
            raise ValueError('الشحن المجاني غير متاح لهذه السلة مؤقتًا؛ يحتاج مراجعة هامش الربح قبل البيع.')
    offer.update(fee=0, discount=standard, floor=MIN_BUNDLE_CONTRIBUTION_PIASTRES,
                 free_shipping=True)
    return offer

def checkout_offer(conn, rows, rate):
    """Compute a verified server-side quote without changing stored prices.

    For one item the customer pays a flat EGP 50 on any supported route, with no
    displayed delivery discount. The actual provider cost is still accounted for.
    New bundle orders may earn less than the legacy 15% margin floor, but must
    cover the supplier product and shipping costs plus a positive contribution.
    """
    if not bundle_enabled():
        free = order_has_free_shipping(rows)
        if free and rate is None:
            raise ValueError('Shipping destination is unavailable')
        legacy = ({'standard': rate[0], 'fee': rate[0], 'discount': 0, 'floor': 0}
                  if free else shipping_offer(conn, rows, rate))
        displayed = sum(displayed_product_price(p) * q for p, q, *_ in rows)
        offer = dict(legacy, displayed_subtotal=displayed,
                     merchandise_total=displayed, bundle_discount=0,
                     bundle_offer_version=0, bundle_unit_count=sum(q for _, q, *_ in rows),
                     free_shipping=False)
        return _apply_free_shipping(offer, rows, rate) if free else offer
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
    # Fixed-offer products are not bundle pieces: no premium and no discount.
    units = sum(q for p, q, *_ in rows if product_in_bundle(p))
    displayed = sum(displayed_product_price(p) * q for p, q, *_ in rows)
    discount = BUNDLE_PREMIUM_PIASTRES * max(units - 1, 0)
    merchandise = displayed - discount
    if units >= 2 or order_has_free_shipping(rows):
        delivery = 0
    else:
        # Flat customer tariff; never advertise the internal carrier subsidy.
        if standard < SINGLE_ITEM_SHIPPING_PIASTRES:
            raise ValueError('Verified shipping tariff is inconsistent with the fixed delivery rate')
        delivery = SINGLE_ITEM_SHIPPING_PIASTRES
    floor = MIN_BUNDLE_CONTRIBUTION_PIASTRES
    supplier_goods = sum(p['supplier_cost'] * q for p, q, *_ in rows)
    if merchandise + delivery - supplier_goods - supplier_shipping < floor:
        raise ValueError('العرض غير متاح لهذه السلة مؤقتًا؛ يحتاج مراجعة هامش الربح قبل البيع.')
    offer = {
        'standard': standard, 'fee': delivery, 'discount': standard - delivery,
        'floor': floor, 'displayed_subtotal': displayed,
        'merchandise_total': merchandise, 'bundle_discount': discount,
        'bundle_offer_version': BUNDLE_OFFER_VERSION, 'bundle_unit_count': units,
        'free_shipping': False,
    }
    return _apply_free_shipping(offer, rows, rate) if order_has_free_shipping(rows) else offer

def validate_promo_snapshot(raw,merchandise,costs,supplier_shipping,buyer_shipping,floor,quantity=None):
    """Reconcile saved voucher terms, totals, limits and merchant contribution.

    Snapshot terms are saved with the order so later admin edits never rewrite
    historical transactions. No live promo table reads are made here.
    """
    code=raw.get('promo_code','') or ''
    percent=raw.get('promo_percent',0)
    discount=raw.get('promo_discount',0)
    shipping=raw.get('promo_shipping_discount',0)
    kind=raw.get('promo_kind','') or ('percent' if code else '')
    value=raw.get('promo_value',0)
    minimum=raw.get('promo_min_items',0) or 1
    maximum=raw.get('promo_max_items',0)
    numbers=(percent,discount,shipping,value,minimum,maximum)
    if not isinstance(code,str) or not all(type(i) is int for i in numbers):
        raise ValueError('Invalid promo order snapshot')
    if any(v<0 for v in (percent,discount,shipping,value,maximum)):
        raise ValueError('Negative promo values are forbidden')
    if not code:
        if kind or percent or discount or shipping or value or maximum:
            raise ValueError('Promo discount has no code')
        return
    if not isinstance(quantity,int) or quantity<minimum or minimum<1:
        raise ValueError('Promo minimum quantity not satisfied')
    baseline_shipping=buyer_shipping+shipping
    if baseline_shipping<0 or shipping>baseline_shipping:
        raise ValueError('Promo shipping snapshot is invalid')
    if kind=='percent':
        value=value or percent  # backward-compatibility for old saved percent-only orders
        if not 1<=value<=100 or percent!=value or maximum:
            raise ValueError('Invalid percentage promo terms')
        expected_product=merchandise*value//100
        expected_shipping=0
    elif kind=='fixed_order':
        if percent or value<=0 or maximum:raise ValueError('Invalid fixed promo terms')
        expected_product=value
        expected_shipping=0
    elif kind=='fixed_item':
        if percent or value<=0 or maximum<0:raise ValueError('Invalid per-unit promo terms')
        expected_product=value*min(quantity,maximum or quantity)
        expected_shipping=0
    elif kind=='shipping_amount':
        if percent or value<=0 or maximum:raise ValueError('Invalid delivery promo terms')
        expected_product=0
        expected_shipping=min(value,baseline_shipping)
    elif kind=='free_shipping':
        if percent or value or maximum:raise ValueError('Invalid free delivery promo terms')
        expected_product=0
        expected_shipping=baseline_shipping
    else:
        raise ValueError('Unknown promo kind')
    if expected_product<=0 and expected_shipping<=0:
        raise ValueError('Coupon has no eligible discount')
    if (discount!=expected_product or shipping!=expected_shipping
        or discount>merchandise):
        raise ValueError('Saved coupon discount was altered')
    if merchandise-discount+buyer_shipping-costs-supplier_shipping < max(100,floor):
        raise ValueError('Coupon would undercut verified supplier costs')


def _free_shipping_flag(raw):
    value=raw.get('free_shipping_applied',0) or 0
    if type(value) is not int or value not in (0,1):
        raise ValueError('Invalid free-shipping snapshot')
    return value==1


def validate_delivery_snapshot(raw,subtotal,costs,quantity=None,bundle_quantity=None):
    if raw.get('bundle_offer_version', 0) in (1, 2, 3):
        standard=raw['shipping_standard_fee']
        supplier=raw['supplier_shipping_piastres']
        buyer=raw['customer_shipping_piastres']
        discount=raw['shipping_discount']
        bundle_discount=raw['bundle_discount']
        units=raw['bundle_unit_count']
        floor=raw['retained_profit_floor']
        promo_ship=raw.get('promo_shipping_discount',0)
        free=_free_shipping_flag(raw)
        if not all(type(v) is int for v in (standard,supplier,buyer,discount,bundle_discount,units,floor,promo_ship)) or promo_ship<0:
            raise ValueError('Invalid promotional order snapshot')
        # Fixed-offer pieces are outside the bundle, so compare with eligible pieces.
        expected_units=quantity if bundle_quantity is None else bundle_quantity
        if units < (0 if free else 1) or expected_units != units or standard != supplier+1000 or not 0 <= buyer <= standard or discount != standard-buyer:
            raise ValueError('Promotional shipping snapshots do not reconcile')
        baseline_buyer=buyer+promo_ship
        if free and (raw['bundle_offer_version'] != 3 or buyer != 0 or promo_ship != 0):
            raise ValueError('Free-shipping product order must have zero customer delivery')
        if bundle_discount != BUNDLE_PREMIUM_PIASTRES*max(units-1,0) or (units>=2 and (buyer!=0 or promo_ship!=0)) or (units==1 and not free and baseline_buyer<100):
            raise ValueError('Promotional discount does not match purchased quantity')
        if raw['bundle_offer_version'] == 3 and units == 1 and not free and baseline_buyer != SINGLE_ITEM_SHIPPING_PIASTRES:
            raise ValueError('Single-item shipping is not the fixed EGP 50 tariff')
        merchandise=subtotal-bundle_discount
        expected_floor = (retained_profit_floor(merchandise)
                          if raw['bundle_offer_version'] == 1
                          else MIN_BUNDLE_CONTRIBUTION_PIASTRES)
        if merchandise < 0 or floor != expected_floor or merchandise+buyer-costs-supplier < floor:
            raise ValueError('Promotional order violates retained merchant contribution')
        validate_promo_snapshot(raw,merchandise,costs,supplier,buyer,floor,quantity)
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
    if _free_shipping_flag(raw):
        # Merchant-funded free delivery for a flagged product: the customer pays
        # no delivery, and goods must still cover supplier goods + delivery.
        if buyer!=0 or discount!=standard or raw.get('promo_shipping_discount',0)!=0:
            raise ValueError('Free-shipping product order must have zero customer delivery')
        if floor!=MIN_BUNDLE_CONTRIBUTION_PIASTRES or subtotal-costs-supplier<floor:
            raise ValueError('Free shipping violates the minimum merchant contribution')
        validate_promo_snapshot(raw,subtotal,costs,supplier,buyer,floor,quantity)
        return
    legacy_discount=discount-raw.get('promo_shipping_discount',0)
    if legacy_discount<0:raise ValueError('Invalid coupon shipping discount')
    if legacy_discount and (floor!=retained_profit_floor(subtotal) or subtotal+buyer-costs-supplier<floor):
        raise ValueError('Delivery discount violates the retained margin')
    validate_promo_snapshot(raw,subtotal,costs,supplier,buyer,floor,quantity)
