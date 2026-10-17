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
    try:
        active=int(payload.get('totalActiveGovernorates'))
    except (ValueError,TypeError):
        raise CatalogValidationError('Missing complete governorate count') from None
    if not isinstance(records,list) or active<1 or len(records)!=active:
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
    # Single DB transaction in the caller: upsert plus cursor checkpoint.
    result=import_products(conn,records,complete=False,source_label='api-admin-batch')
    # Unverified stock: imported Safka merchandise stays unpublished.
    conn.execute("UPDATE products SET active=0 WHERE source='safka'")
    next_page=page+1 if page<pages else 1
    conn.execute(
        "INSERT INTO store_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ('safka_admin_next_page',str(next_page)))
    return {'imported':result['updated'],'next_page':next_page,'total_pages':pages,
            'cycle_complete':page==pages}


def sync_shipping_snapshot(conn, flattened):
    # Atomic replacement of Safka-only rates; manual shipping and orders untouched.
    return import_shipping(conn,flattened,source_label='api-admin-verified')
