"""Safka bridge: local catalog/shipping normalization and safe transactional staging.

No requests are made on import. Real endpoint URLs and order payload shape must be
verified against the marketer's actual Safka credentials before enabling writes.
"""
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlparse
from profit_pricing import preserve_market_price, init_market_schema
from catalog_quality import clean_description, suggested_category, availability_from_source, extract_rating

PROVIDER = 'safka'
TEN_EGP_PIASTRES = 1000
SHIPPING_MARKUP_PIASTRES = 1000


class CatalogValidationError(ValueError):
    pass


def human_product_slug(title):
    value = unicodedata.normalize('NFKC', str(title or '')).lower()
    value = re.sub(r'[^\w\u0600-\u06ff]+', '-', value, flags=re.UNICODE)
    value = re.sub(r'[_-]+', '-', value).strip('-')[:72].strip('-')
    return value or 'shoppy-store'


def free_product_slug(conn, title):
    base = human_product_slug(title)
    for n in range(1, 100000):
        slug = base if n == 1 else base + '-' + str(n)
        exists = conn.execute('SELECT id FROM products WHERE slug=?', (slug,)).fetchone()
        alias = conn.execute('SELECT product_id FROM product_slug_aliases WHERE slug=?', (slug,)).fetchone()
        if not exists and not alias:
            return slug
    raise ValueError('No free product URL')


def migrate_supplier_product_slugs(conn):
    """Keep redirects for past advertising product URLs."""
    conn.execute("""CREATE TABLE IF NOT EXISTS product_slug_aliases(
        slug TEXT PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE)""")
    rows = conn.execute("SELECT id,title,slug FROM products WHERE source='safka' AND slug LIKE 'safka-%' ORDER BY id").fetchall()
    for row in rows:
        friendly = free_product_slug(conn, row['title'])
        conn.execute("INSERT INTO product_slug_aliases(slug,product_id) VALUES(?,?) ON CONFLICT(slug) DO NOTHING",
                     (row['slug'], row['id']))
        conn.execute("UPDATE products SET slug=? WHERE id=? AND slug=?", (friendly, row['id'], row['slug']))
    return len(rows)


def egp_to_piastres(value):
    if isinstance(value, bool):
        raise CatalogValidationError('Boolean is not a valid price')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CatalogValidationError('Missing or invalid supplier price') from None
    if not n.is_finite() or n < 0 or n > 100000000:
        raise CatalogValidationError('Supplier price is out of range')
    return int((n * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def sale_price_piastres(base_piastres):
    """Supplier price + one third, ALWAYS rounded UP to a full EGP 10.

    Integer arithmetic avoids floating-point errors. Existing order snapshots
    are immutable and are never repriced by this function.
    """
    if isinstance(base_piastres, bool) or not isinstance(base_piastres, int) or base_piastres < 0:
        raise CatalogValidationError('Invalid base price')
    denominator = 3 * TEN_EGP_PIASTRES
    return ((4 * base_piastres + denominator - 1) // denominator) * TEN_EGP_PIASTRES


def customer_shipping_piastres(supplier_piastres):
    if not isinstance(supplier_piastres, int) or supplier_piastres < 0:
        raise CatalogValidationError('Invalid supplier shipping')
    return supplier_piastres + SHIPPING_MARKUP_PIASTRES


def _first(d, *keys):
    for k in keys:
        if k in d and d[k] is not None and d[k] != '':
            return d[k]
    return None


def _text(value, limit=5000):
    if isinstance(value, dict):
        value = _first(value, 'ar', 'arabic', 'name', 'title', 'en')
    if not isinstance(value, str):
        return ''
    return value.strip()[:limit]


def _category_text(value):
    """Keep the supplied category/path; never generate an invented classification."""
    if isinstance(value, list):
        parts = [_category_text(v) for v in value]
        return ' / '.join(v for v in parts if v)
    if isinstance(value, str):
        return value.strip()[:200]
    if isinstance(value, dict):
        parent = _category_text(_first(value, 'parent', 'parent_category'))
        name = _text(_first(value, 'name', 'title', 'name_ar'), 100)
        return ' / '.join(v for v in (parent, name) if v)
    return ''


def _urls(raw):
    if not raw:
        return []
    if not isinstance(raw, list):
        raw = [raw]
    result = []
    for item in raw:
        if isinstance(item, dict):
            item = _first(item, 'url', 'image_url', 'src', 'path')
        if not isinstance(item, str):
            continue
        p = urlparse(item)
        if p.scheme == 'https' and p.hostname and not p.username and not p.password and len(item) < 2000 and item not in result:
            result.append(item)
    return result[:30]


def _stock(raw):
    qty = _first(raw, 'quantity', 'stock_quantity', 'available_quantity', 'stock', 'qty')
    if isinstance(qty, dict):
        qty = _first(qty, 'quantity', 'available')
    if qty is None:
        return None
    if isinstance(qty, bool) or not isinstance(qty, (int, str)):
        raise CatalogValidationError('Unrecognized stock quantity type')
    try:
        result = int(qty)
    except (TypeError, ValueError):
        raise CatalogValidationError('Invalid stock quantity') from None
    if result < 0 or result > 100000000:
        raise CatalogValidationError('Invalid stock range')
    return result


def normalize_product(raw):
    """Map a *verified* supplier JSON record; incomplete records are rejected.

    Field names must be matched against the actual Safka API response before live
    syncing. Supported aliases help with local fixtures, not a guarantee of API shape.
    """
    if not isinstance(raw, dict):
        raise CatalogValidationError('Product is not an object')
    supplier_id = _first(raw, 'id', 'product_id', '_id')
    if isinstance(supplier_id, bool) or not isinstance(supplier_id, (str, int)) or not str(supplier_id).strip():
        raise CatalogValidationError('Supplier product ID missing')
    sid = str(supplier_id).strip()
    if len(sid) > 100:
        raise CatalogValidationError('Supplier ID too long')
    # The provider's internal product ID and barcode/SKU may be different.
    # Never substitute one for the other when preparing a future supplier order.
    code_raw = _first(raw, 'barcode', 'product_code', 'sku')
    if code_raw is not None and (isinstance(code_raw, bool) or not isinstance(code_raw, (str,int))):
        raise CatalogValidationError('Supplier product code type is invalid')
    supplier_code = str(code_raw).strip() if code_raw is not None else None
    if supplier_code is not None and (not supplier_code or len(supplier_code) > 100):
        raise CatalogValidationError('Invalid supplier product code')
    title = _text(_first(raw, 'name', 'title', 'product_name', 'name_ar'), 180)
    if not title:
        raise CatalogValidationError('Supplier product name missing')
    cost = egp_to_piastres(_first(raw, 'sale_price', 'price', 'product_price', 'base_price'))
    if cost == 0:
        raise CatalogValidationError('Refusing an unpriced supplier product')
    category = _category_text(_first(raw, 'category_path', 'categories', 'category', 'category_name'))
    if not category:
        # Safka public Products List currently omits categories. Explicitly mark
        # as unclassified; do not invent a category or publish without stock.
        category = suggested_category(title)
    qty = _stock(raw)
    desc = clean_description(_text(_first(raw, 'description', 'product_description', 'description_ar'), 20000))
    images = _urls(_first(raw, 'images', 'product_images', 'image_urls', 'image', 'image_url'))
    videos = _urls(_first(raw, 'videos', 'video_urls', 'video_url'))
    options = _first(raw, 'options', 'attributes', 'variants')
    # Variant schema must be verified; do not silently pretend choices are available.
    if options is None:
        options = {}
    elif isinstance(options, dict) and all(isinstance(k, str) and isinstance(v, list)
                                                and all(isinstance(x, str) for x in v) for k, v in options.items()):
        options = {k[:48]: list(dict.fromkeys(v))[:30] for k, v in options.items()}
    else:
        raise CatalogValidationError('Supplier variants require an explicit field mapping')
    properties=raw.get('properties')
    if isinstance(properties,list) and properties:
        available=[p for p in properties if isinstance(p,dict)
                   and p.get('is_available') is not False and p.get('is_active') is not False
                   and p.get('value')!=0]
        counts=[p.get('value') for p in available]
        if qty is None and all(isinstance(n,int) and not isinstance(n,bool) and n>=0 for n in counts):
            qty=sum(counts)
        # Equal-priced supplier choices can use the existing choice field safely.
        # Different-price properties must not be guessed from the parent price.
        if not options and len(available)>1:
            labels=[p.get('key') for p in available]
            if (all(isinstance(label,str) and label.strip() for label in labels)
                and len(set(labels))==len(labels)
                and all(egp_to_piastres(p.get('sale_price',cost/100))==cost for p in available)):
                options={'الاختيار':labels}
    return {
        'supplier_id': sid, 'supplier_code': supplier_code, 'title': title, 'description': desc, 'category': category,
        'supplier_cost': cost, 'price': sale_price_piastres(cost), 'stock': qty,
        'images': images, 'videos': videos, 'options': options,
        'supplier_available': availability_from_source(raw,qty),
        'supplier_rating': extract_rating(raw)[0], 'supplier_review_count': extract_rating(raw)[1],
    }


def expand_safka_shipping(records):
    """Turn each documented price-list governorate into its supported city rows."""
    output = []
    for item in records:
        if not isinstance(item, dict):
            raise CatalogValidationError('Invalid price-list record')
        cities = item.get('cities')
        if cities is None:
            output.append(item)
            continue
        if not isinstance(cities, list) or not cities or len(cities) > 1000:
            raise CatalogValidationError('Missing or invalid supported cities')
        for city in cities:
            if not isinstance(city, dict):
                raise CatalogValidationError('Invalid supported city')
            name = _text(_first(city, 'city_name_ar', 'cityNameAr'), 80)
            if not name:
                raise CatalogValidationError('Missing Arabic city name')
            entry = dict(item)
            entry.pop('cities')
            entry['city'] = name
            entry['city_id'] = str(city.get('id', '')).strip()
            output.append(entry)
    return output


def normalize_shipping(raw):
    if not isinstance(raw, dict):
        raise CatalogValidationError('Shipping entry is not an object')
    gov = _text(_first(raw, 'governorateNameAr', 'governorate', 'governorate_name', 'province'), 80)
    # Safka price-list may contain nested cities. Do not collapse city-specific
    # pricing into a governorate-wide fee before the nested schema is verified.
    if 'cities' in raw:
        raise CatalogValidationError('Expand nested Safka city list first')
    city = _text(_first(raw, 'city', 'city_name', 'markaz'), 80)
    area = _text(_first(raw, 'area', 'area_name', 'district'), 80)
    price = egp_to_piastres(_first(raw, 'shipping_fee', 'fee', 'price', 'shipping_price'))
    if not gov:
        raise CatalogValidationError('Shipping governorate missing')
    # Safka Create Order requires a price-list document ID, never a province name.
    # Only accept an explicit document ID field, or a verified price-list record _id.
    raw_id = _first(raw, 'pricing_document_id', 'price_list_document_id', 'document_id', '_id')
    if raw_id is not None and (isinstance(raw_id, bool) or not isinstance(raw_id, (str, int))):
        raise CatalogValidationError('Invalid Safka pricing document ID')
    pricing_id = str(raw_id).strip() if raw_id is not None else None
    if pricing_id is not None and (not pricing_id or len(pricing_id) > 100):
        raise CatalogValidationError('Invalid Safka pricing document ID')
    city_id = raw.get('city_id')
    if city_id is not None and (isinstance(city_id, bool) or not isinstance(city_id, (str,int))):
        raise CatalogValidationError('Invalid supplier city ID')
    return {'governorate': gov, 'city': city, 'area': area,
            'city_id': str(city_id).strip() if city_id is not None else None,
            'supplier_fee': price, 'fee': customer_shipping_piastres(price),
            'pricing_document_id': pricing_id}


def init_safka_schema(conn):
    init_market_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS product_slug_aliases(
        slug TEXT PRIMARY KEY,
        product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE)""")
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(products)')}
    for key, definition in (
        ('source', "TEXT NOT NULL DEFAULT 'manual'"),
        ('supplier_product_id', 'TEXT'),
        ('supplier_product_code', 'TEXT'),
        ('supplier_stock', 'INTEGER'),
        ('supplier_last_sync', 'TEXT'),
    ):
        if key not in cols:
            conn.execute(f'ALTER TABLE products ADD COLUMN {key} {definition}')
    for key,definition in (
        ('supplier_available','INTEGER'),('supplier_rating','REAL'),
        ('supplier_review_count','INTEGER'),('supplier_listed','INTEGER NOT NULL DEFAULT 1'),
        ('supplier_catalog_generation','TEXT')
    ):
        if key not in cols:
            conn.execute(f'ALTER TABLE products ADD COLUMN {key} {definition}')
    conn.executescript('''
    CREATE UNIQUE INDEX IF NOT EXISTS idx_supplier_product ON products(source,supplier_product_id);
    CREATE TABLE IF NOT EXISTS product_overrides (
      product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
      title TEXT NOT NULL, description TEXT NOT NULL, category TEXT NOT NULL,
      price INTEGER NOT NULL, options_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS safka_shipping_rates (
      id INTEGER PRIMARY KEY,
      governorate TEXT NOT NULL, city TEXT NOT NULL DEFAULT '', area TEXT NOT NULL DEFAULT '',
      supplier_fee INTEGER NOT NULL, fee INTEGER NOT NULL, updated_at TEXT NOT NULL,
      UNIQUE(governorate,city,area));
    CREATE TABLE IF NOT EXISTS safka_sync_runs (
      id INTEGER PRIMARY KEY,kind TEXT NOT NULL, updated INTEGER NOT NULL,
      hidden INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
      source_label TEXT NOT NULL DEFAULT 'local-json');
    CREATE TABLE IF NOT EXISTS safka_order_outbox (
      id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id),
      status TEXT NOT NULL DEFAULT 'awaiting_provider_configuration',
      supplier_order_id TEXT, error TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
      attempt_count INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS safka_schema_meta (
      key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    # Additive migration, preserving existing price-list data and prior orders.
    shipping_cols = {r['name'] for r in conn.execute('PRAGMA table_info(safka_shipping_rates)')}
    if 'pricing_document_id' not in shipping_cols:
        conn.execute('ALTER TABLE safka_shipping_rates ADD COLUMN pricing_document_id TEXT')
    if 'city_id' not in shipping_cols:
        conn.execute('ALTER TABLE safka_shipping_rates ADD COLUMN city_id TEXT')
    order_cols = {r['name'] for r in conn.execute('PRAGMA table_info(orders)')}
    if 'safka_shipping_pricing_document_id' not in order_cols:
        conn.execute('ALTER TABLE orders ADD COLUMN safka_shipping_pricing_document_id TEXT')
    if 'safka_city_id' not in order_cols:
        conn.execute('ALTER TABLE orders ADD COLUMN safka_city_id TEXT')
    outbox_cols = {r['name'] for r in conn.execute('PRAGMA table_info(safka_order_outbox)')}
    if 'submitted_commission_piastres' not in outbox_cols:
        conn.execute('ALTER TABLE safka_order_outbox ADD COLUMN submitted_commission_piastres INTEGER')
    for column in ('supplier_status','supplier_status_updated_at'):
        if column not in outbox_cols:
            conn.execute('ALTER TABLE safka_order_outbox ADD COLUMN '+column+' TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_safka_order_provider_id ON safka_order_outbox(supplier_order_id)')
    # One-time correction for products imported by V4.1 (nearest ten).
    # Never update order_items / orders: those are immutable price snapshots.
    migrated = conn.execute("SELECT value FROM safka_schema_meta WHERE key='product_price_rule'").fetchone()
    if migrated is None or migrated['value'] != 'one_third_round_up_10_v42':
        for product in conn.execute("SELECT id,supplier_cost FROM products WHERE source='safka' AND supplier_cost IS NOT NULL").fetchall():
            new_price = sale_price_piastres(product['supplier_cost'])
            conn.execute('UPDATE products SET price=? WHERE id=?', (new_price, product['id']))
        conn.execute("INSERT INTO safka_schema_meta(key,value) VALUES('product_price_rule','one_third_round_up_10_v42') ON CONFLICT(key) DO UPDATE SET value=excluded.value")


def import_products(conn, records, *, complete=False, source_label='local-json'):
    """Atomic upsert. Reject unknown item schema without changing catalog.

    Hiding deleted supplier products is enabled ONLY if caller has verified a complete,
    successfully paginated upstream catalog (never a single API page).
    """
    rows = [normalize_product(x) for x in records]
    supplier_ids = [p['supplier_id'] for p in rows]
    if len(set(supplier_ids)) != len(supplier_ids):
        raise CatalogValidationError('Duplicate supplier ID in catalog')
    updated = 0
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('SAVEPOINT safka_catalog')
    try:
        for p in rows:
            old = conn.execute("SELECT id,image FROM products WHERE source='safka' AND supplier_product_id=?", (p['supplier_id'],)).fetchone()
            # Safka list membership is the marketer's availability signal (not a guaranteed stock count).
            active = 1 if p['supplier_available'] is not False and (p['stock'] is None or p['stock']>0) else 0
            if old:
                pid = old['id']
                conn.execute('''UPDATE products SET title=?,description=?,category=?,price=?,supplier_cost=?,
                     supplier_stock=?,supplier_product_code=?,supplier_last_sync=?,active=?,options_json=?,image=?,
                     supplier_available=?,supplier_rating=?,supplier_review_count=? WHERE id=?''',
                     (p['title'],p['description'],p['category'][:200],p['price'],p['supplier_cost'],p['stock'],p['supplier_code'],now,
                      active,json.dumps(p['options'],ensure_ascii=False),p['images'][0] if p['images'] else '',
                      None if p['supplier_available'] is None else int(p['supplier_available']),
                      p['supplier_rating'],p['supplier_review_count'],pid))
                conn.execute("UPDATE products SET supplier_listed=1 WHERE id=?",(pid,))
            else:
                # Stable readable name; collisions get numeric suffixes.
                slug = free_product_slug(conn, p['title'])
                cur = conn.execute('''INSERT INTO products(title,slug,description,price,image,active,options_json,category,
                                    supplier_cost,source,supplier_product_id,supplier_product_code,supplier_stock,supplier_last_sync,supplier_available,supplier_rating,supplier_review_count)
                                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                                   (p['title'],slug,p['description'],p['price'],p['images'][0] if p['images'] else '',active,
                                    json.dumps(p['options'],ensure_ascii=False),p['category'][:200],p['supplier_cost'],
                                    'safka',p['supplier_id'],p['supplier_code'],p['stock'],now,
                                    None if p['supplier_available'] is None else int(p['supplier_available']),
                                    p['supplier_rating'],p['supplier_review_count']))
                pid = cur.lastrowid
            # Merchant edits are kept over supplier changes during every re-sync.
            custom=conn.execute('SELECT * FROM product_overrides WHERE product_id=?',(pid,)).fetchone()
            if custom:
                conn.execute('UPDATE products SET title=?,description=?,category=?,price=?,options_json=? WHERE id=?',
                             (custom['title'],custom['description'],custom['category'],custom['price'],custom['options_json'],pid))
            preserve_market_price(conn,pid,p['supplier_cost'])
            # Only replace remote provider media. Never remove customer-uploaded local images.
            conn.execute("DELETE FROM product_media WHERE product_id=? AND path LIKE 'https://%'", (pid,))
            for index, (kind, url) in enumerate([('image',u) for u in p['images']] + [('video',u) for u in p['videos']]):
                conn.execute('INSERT INTO product_media(product_id,kind,path,position) VALUES(?,?,?,?)',(pid,kind,url,index))
            updated += 1
        hidden = 0
        if complete:
            # Complete snapshot only; empty catalogs require explicit supervisor action.
            if not rows:
                raise CatalogValidationError('Refusing to remove all supplier products from an empty catalog')
            placeholders = ','.join('?' for _ in supplier_ids)
            sql = f"SELECT COUNT(*) FROM products WHERE source='safka' AND supplier_product_id NOT IN ({placeholders}) AND active=1"
            hidden = conn.execute(sql,supplier_ids).fetchone()[0]
            conn.execute(f"UPDATE products SET active=0 WHERE source='safka' AND supplier_product_id NOT IN ({placeholders})",supplier_ids)
        conn.execute('INSERT INTO safka_sync_runs(kind,updated,hidden,created_at,source_label) VALUES(?,?,?,?,?)',
                     ('products',updated,hidden,now,source_label[:100]))
        conn.execute('RELEASE SAVEPOINT safka_catalog')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT safka_catalog')
        conn.execute('RELEASE SAVEPOINT safka_catalog')
        raise
    return {'updated':updated,'hidden':hidden}


def import_shipping(conn, records, *, source_label='local-json'):
    rows = [normalize_shipping(x) for x in records]
    if len({(r['governorate'],r['city'],r['area']) for r in rows}) != len(rows):
        raise CatalogValidationError('Duplicate destination in Safka rate list')
    if not rows:
        raise CatalogValidationError('Empty shipping data cannot replace live rates')
    now = datetime.now(timezone.utc).isoformat()
    conn.execute('SAVEPOINT safka_rates')
    try:
        conn.execute('DELETE FROM safka_shipping_rates')
        for r in rows:
            conn.execute('INSERT INTO safka_shipping_rates(governorate,city,area,supplier_fee,fee,updated_at,pricing_document_id,city_id) VALUES(?,?,?,?,?,?,?,?)',
                         (r['governorate'],r['city'],r['area'],r['supplier_fee'],r['fee'],now,r['pricing_document_id'],r['city_id']))
        conn.execute('INSERT INTO safka_sync_runs(kind,updated,hidden,created_at,source_label) VALUES(?,?,?,?,?)',
                     ('shipping',len(rows),0,now,source_label[:100]))
        conn.execute('RELEASE SAVEPOINT safka_rates')
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT safka_rates')
        conn.execute('RELEASE SAVEPOINT safka_rates')
        raise
    return {'updated':len(rows)}


def shipping_pricing_document_id(conn, gov, city, area, customer_fee, supplier_fee):
    # The pricing ID selected at checkout, before any supplier price changes.
    gov,city,area=(s.strip() for s in (gov,city,area))
    for c,a in ((city,area),(city,''),('','')):
        row=conn.execute('SELECT fee,supplier_fee,pricing_document_id FROM safka_shipping_rates WHERE governorate=? AND city=? AND area=?',
                         (gov,c,a)).fetchone()
        if row:
            if row['fee'] != customer_fee or row['supplier_fee'] != supplier_fee:
                return None
            return row['pricing_document_id']
    return None


def quote_safka_shipping(conn,gov,city='',area=''):
    gov,city,area=(s.strip() for s in (gov,city,area))
    for c,a in ((city,area),(city,''),('','')):
        if a and not c:
            continue
        row=conn.execute('''SELECT fee,supplier_fee FROM safka_shipping_rates
                            WHERE governorate=? AND city=? AND area=?''',(gov,c,a)).fetchone()
        if row:
            return row['fee'],row['supplier_fee']
    return None


def queue_safka_order(conn, order_id):
    """Stage a single durable event. Does not send order to provider."""
    if not conn.execute('SELECT 1 FROM orders WHERE id=?',(order_id,)).fetchone():
        raise CatalogValidationError('Unknown order')
    source_rows=conn.execute('''SELECT p.source,p.supplier_product_id FROM order_items oi
                                  LEFT JOIN products p ON p.id=oi.product_id
                                  WHERE oi.order_id=?''',(order_id,)).fetchall()
    if not source_rows or any(r['source']!='safka' or not r['supplier_product_id'] for r in source_rows):
        return False
    now=datetime.now(timezone.utc).isoformat()
    conn.execute('''INSERT OR IGNORE INTO safka_order_outbox(order_id,created_at,updated_at)
                    VALUES(?,?,?)''',(order_id,now,now))
    return True


def order_preview(conn, order_id):
    """Private snapshot for mapping to the supplier's *verified* API order schema."""
    o=conn.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
    if not o:
        raise CatalogValidationError('Unknown order')
    items=conn.execute('''SELECT oi.title,oi.quantity,oi.unit_price,oi.unit_cost,oi.variant_json,
                                p.supplier_product_id,p.supplier_product_code,p.source
                         FROM order_items oi LEFT JOIN products p ON oi.product_id=p.id
                         WHERE oi.order_id=?''',(order_id,)).fetchall()
    if not items or any(i['source']!='safka' or not i['supplier_product_id'] for i in items):
        raise CatalogValidationError('Order contains non-Safka or missing supplier product')
    # Re-resolve the exact rate used at checkout and reject changed rate snapshots.
    # A price-list document ID is mandatory to build an official Create Order payload.
    pieces = [part.strip() for part in o['address'].split(' | ', 2)]
    city = pieces[0] if len(pieces) >= 3 else ''
    area = pieces[1] if len(pieces) >= 3 else ''
    pricing_id = o['safka_shipping_pricing_document_id']
    # Old orders lack a snapshot. Match a current rate ONLY if the price exactly matches.
    for target_city, target_area in ((city, area), (city, ''), ('', '')):
        matched = conn.execute('''SELECT supplier_fee,fee,pricing_document_id FROM safka_shipping_rates
            WHERE governorate=? AND city=? AND area=?''', (o['governorate'],target_city,target_area)).fetchone()
        if matched:
            if not pricing_id and matched['fee'] == o['shipping_fee'] and matched['supplier_fee'] == o['shipping_supplier_cost']:
                pricing_id = matched['pricing_document_id']
            break
    return {
        'shipping_pricing_document_id': pricing_id,
        'supplier_city_id': o['safka_city_id'],
        'merchant_reference':o['reference'], 'customer':o['customer'], 'phone':o['phone'],
        'governorate':o['governorate'],'address':o['address'],
        'customer_shipping_piastres':o['shipping_fee'],
        'shipping_standard_fee':o['shipping_standard_fee'] if 'shipping_standard_fee' in o.keys() else None,
        'shipping_discount':o['shipping_discount'] if 'shipping_discount' in o.keys() else 0,
        'retained_profit_floor':o['retained_profit_floor'] if 'retained_profit_floor' in o.keys() else 0,
        'bundle_discount':o['bundle_discount'] if 'bundle_discount' in o.keys() else 0,
        'bundle_offer_version':o['bundle_offer_version'] if 'bundle_offer_version' in o.keys() else 0,
        'bundle_unit_count':o['bundle_unit_count'] if 'bundle_unit_count' in o.keys() else 0,
        'supplier_shipping_piastres':o['shipping_supplier_cost'],
        'customer_total_piastres':o['total'],
        'items':[{'supplier_product_id':x['supplier_product_id'],'supplier_product_code':x['supplier_product_code'],'name':x['title'],
                  'quantity':x['quantity'],'customer_unit_price_piastres':x['unit_price'],
                  'supplier_unit_price_piastres':x['unit_cost'],
                  'choices':json.loads(x['variant_json'] or '{}')} for x in items]
    }


def supplier_stock_ready(p, qty=1):
    """Don't advertise stale or unverified Safka inventory as orderable."""
    if p['source']!='safka':
        return True
    if p['active'] != 1:
        return False
    if 'supplier_listed' in p.keys() and p['supplier_listed'] == 0:
        return False
    if 'supplier_available' in p.keys() and p['supplier_available'] == 0:
        return False
    count=p['supplier_stock']
    # A supplier listing can be ordered without publishing an invented quantity.
    if count is not None and count < qty:
        return False
    stamp = p['supplier_last_sync']
    if not stamp:
        return False
    try:
        last=datetime.fromisoformat(stamp)
        if last.tzinfo is None:
            return False
        age=datetime.now(timezone.utc)-last
        import os
        minutes=int(os.getenv('SHOPY_SAFKA_MAX_STOCK_AGE_MINUTES','5760'))
        return timedelta(0)<=age<=timedelta(minutes=max(5,min(minutes,10080)))
    except (ValueError,TypeError):
        return False
