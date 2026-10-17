"""Owner-triggered Safka read + validated data synchronization.

No scheduler, no supplier order POST, and no untrusted target host.
"""
import json
import urllib.parse
import urllib.request
from safka_import import _SAFE_OPENER, _extract_list
from safka_bridge import (CatalogValidationError, expand_safka_shipping,
                          normalize_product, normalize_shipping, import_products, import_shipping)

HOST = 'https://api.safka-eg.com'
LIMIT_BYTES = 4_000_000


def api_page(key, path, kind, *, page=1, size=50, timeout=8):
    if kind not in ('products', 'shipping'):
        raise CatalogValidationError('Unsupported Safka resource')
    if (not path.startswith('/api/v1/public/') or '?' in path or '#' in path
            or '//' in path or '\\' in path):
        raise CatalogValidationError('Use an exact documented Safka public API path')
    expected = '/api/v1/public/products' if kind=='products' else '/api/v1/public/price-list'
    if path != expected:
        raise CatalogValidationError('Unexpected Safka read endpoint')
    if not isinstance(page,int) or page < 1 or page > 10000 or size < 1 or size > 50:
        raise CatalogValidationError('Invalid page request')
    query = urllib.parse.urlencode({'page':page,'size':size})
    req = urllib.request.Request(
        HOST + path + '?' + query,
        headers={'api-safka-key':key, 'Accept':'application/json',
                 'User-Agent':'ShopyStoreIntegration/1.0'},method='GET')
    with _SAFE_OPENER.open(req, timeout=timeout) as response:
        blob = response.read(LIMIT_BYTES + 1)
    if len(blob) > LIMIT_BYTES:
        raise CatalogValidationError('Supplier response too large')
    payload = json.loads(blob)
    if isinstance(payload,dict) and payload.get('success') is False:
        raise CatalogValidationError('Supplier reported API failure')
    records = _extract_list(payload,kind)
    if not isinstance(payload,dict):
        raise CatalogValidationError('Missing API pagination envelope')
    return payload, records


def checked_products(payload, records, requested_page):
    try:
        current=int(payload.get('currentPage'))
        pages=int(payload.get('pages'))
    except (TypeError,ValueError):
        raise CatalogValidationError('Missing official products pagination') from None
    if current != requested_page or pages < 1 or pages > 10000 or current > pages:
        raise CatalogValidationError('Inconsistent supplier pagination')
    if not isinstance(records,list) or len(records)>50 or not records:
        raise CatalogValidationError('Unexpected or empty product page')
    mapped=[normalize_product(row) for row in records]
    if len({x['supplier_id'] for x in mapped})!=len(mapped):
        raise CatalogValidationError('Duplicate supplier product ID in page')
    return pages, len(mapped)


def checked_shipping(payload, records):
    # Safka's price-list has been observed returning all 27 governorates
    # in one response despite a larger reported page count. Verify actual
    # itemsCount (or totalActiveGovernorates) before atomically replacing data.
    total=payload.get('totalActiveGovernorates',payload.get('itemsCount'))
    try:
        active=int(total)
    except (ValueError,TypeError):
        raise CatalogValidationError('Missing authoritative governorate count') from None
    if not isinstance(records,list) or active<1 or active>27 or len(records)!=active:
        raise CatalogValidationError('Incomplete Safka price-list snapshot; no shipping changed')
    if any(row.get('is_active') is False for row in records if isinstance(row,dict)):
        raise CatalogValidationError('Price-list contains inactive governorates')
    flattened=expand_safka_shipping(records)
    if not flattened or len(flattened)>10000:
        raise CatalogValidationError('Invalid expanded city count')
    normalized=[normalize_shipping(row) for row in flattened]
    pairs=[(r['governorate'],r['city'],r['area']) for r in normalized]
    if len(set(pairs))!=len(pairs):
        raise CatalogValidationError('Duplicate city price-list entry')
    if any(not r['city'] or not r['pricing_document_id'] for r in normalized):
        raise CatalogValidationError('Missing verified city or price-list ID')
    return flattened


def sync_product_page(conn, records, page, pages):
    """Resumable full-catalog reconciliation: replace the visible supplier listing
    only AFTER all pages have been verified and imported successfully.

    Removes unavailable Safka items from public search with a soft archive.
    Never physically deletes products referenced by historical orders.
    """
    import uuid
    if not isinstance(page,int) or not 1<=page<=pages<=10000:
        raise CatalogValidationError('Invalid supplier pagination')
    normalized=[normalize_product(record) for record in records]
    ids=[row['supplier_id'] for row in normalized]
    if not ids or len(set(ids))!=len(ids):
        raise CatalogValidationError('Duplicate or missing Safka product ID')
    conn.execute('SAVEPOINT safka_full_sync')
    try:
        def setting(name):
            row=conn.execute('SELECT value FROM store_settings WHERE key=?',(name,)).fetchone()
            return row['value'] if row else None
        def set_setting(name,value):
            conn.execute("""INSERT INTO store_settings(key,value) VALUES(?,?)
                            ON CONFLICT(key) DO UPDATE SET value=excluded.value""",(name,str(value)))
        if page==1:
            # Start a new generation only at page one; retries are idempotent.
            generation=uuid.uuid4().hex
            set_setting('safka_cycle_generation',generation)
            set_setting('safka_cycle_total_pages',pages)
        else:
            generation=setting('safka_cycle_generation')
            declared_pages=setting('safka_cycle_total_pages')
            if not generation or declared_pages!=str(pages):
                raise CatalogValidationError('Supplier catalog pages changed; restart from first page')
        result=import_products(conn,records,complete=False,source_label='api-full-catalog')
        placeholders=','.join('?' for _ in ids)
        conn.execute('UPDATE products SET supplier_catalog_generation=?,supplier_listed=1 WHERE source=? AND supplier_product_id IN ('+placeholders+')',
                     [generation,'safka',*ids])
        complete=page==pages
        archived=0
        if complete:
            # In one DB transaction, archive every Safka-only product missing
            # from this verified full generation. Keep old order snapshots intact.
            missing=conn.execute("""SELECT COUNT(*) FROM products WHERE source='safka'
                 AND (supplier_catalog_generation IS NULL OR supplier_catalog_generation!=?)""",(generation,)).fetchone()[0]
            conn.execute("""UPDATE products SET supplier_listed=0,active=0 WHERE source='safka'
                 AND (supplier_catalog_generation IS NULL OR supplier_catalog_generation!=?)""",(generation,))
            archived=missing
            set_setting('safka_cycle_generation','')
            set_setting('safka_cycle_total_pages','')
        next_page=1 if complete else page+1
        set_setting('safka_admin_next_page',next_page)
        conn.execute('RELEASE SAVEPOINT safka_full_sync')
        return {'imported':result['updated'],'next_page':next_page,
                'total_pages':pages,'cycle_complete':complete,'archived':archived}
    except Exception:
        conn.execute('ROLLBACK TO SAVEPOINT safka_full_sync')
        conn.execute('RELEASE SAVEPOINT safka_full_sync')
        raise


def sync_shipping_snapshot(conn, flattened):
    # Atomic replacement of Safka-only rates; manual shipping and orders untouched.
    return import_shipping(conn,flattened,source_label='api-admin-verified')
