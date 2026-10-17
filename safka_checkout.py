"""Customer checkout reads Safka; never creates a supplier order from a GET."""
import os
import time
import json
from datetime import datetime, timezone
from safka_admin_sync import api_page, checked_shipping, sync_shipping_snapshot
from safka_bridge import CatalogValidationError
from safka_bridge import import_products
from safka_webhooks import fetch_product_detail

SHIPPING_TTL = 3600


def refresh_product(db, product, *, detail_fetch=None, force=False):
    """Read current properties on product pages, with a five-minute DB cache."""
    if product['source']!='safka' or os.getenv('SHOPY_SAFKA_READ_ENABLED')!='1':
        return product
    if not force and product['supplier_last_sync']:
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(product['supplier_last_sync'])).total_seconds()
            if 0<=age<300:
                return product
        except (TypeError,ValueError):
            pass
    detail_fetch=detail_fetch or fetch_product_detail
    _,raw=detail_fetch(os.getenv('SHOPY_SAFKA_API_KEY','').strip(),product['supplier_product_id'])
    with db() as conn:
        import_products(conn,[raw],source_label='checkout-product-detail')
        return conn.execute('SELECT * FROM products WHERE id=?',(product['id'],)).fetchone()


def shipping_snapshot(key, *, read=api_page):
    """Handle both real pagination and the provider's all-in-one price list."""
    records = {}
    expected = None
    for page in range(1, 28):
        envelope, rows = read(key, '/api/v1/public/price-list', 'shipping', page=page, size=50)
        count = envelope.get('totalActiveGovernorates', envelope.get('itemsCount'))
        if isinstance(count, bool):
            raise CatalogValidationError('Invalid governorate count')
        try:
            count = int(count)
        except (ValueError, TypeError):
            raise CatalogValidationError('Missing governorate count') from None
        if not 1 <= count <= 27 or expected not in (None, count):
            raise CatalogValidationError('Shipping snapshot changed during refresh')
        expected = count
        before = len(records)
        for row in rows:
            if not isinstance(row, dict) or not row.get('_id'):
                raise CatalogValidationError('Missing shipping price-list ID')
            ident = row['_id']
            if ident in records and records[ident] != row:
                raise CatalogValidationError('Conflicting shipping rates')
            records[ident] = row
        if len(records) == expected:
            flattened = checked_shipping({'itemsCount':expected}, list(records.values()))
            if any(not row.get('city_id') for row in flattened):
                raise CatalogValidationError('Missing official city IDs')
            return flattened
        if len(records) > expected or len(records) == before:
            break
    raise CatalogValidationError('Incomplete supplier shipping snapshot')


def ensure_shipping(db, *, read=api_page):
    """Refresh a bounded, shared cache. Failed reads preserve previous rates.

    A compare-and-swap attempt timestamp limits provider calls across workers.
    API credentials stay in the deployed server environment.
    """
    key = os.getenv('SHOPY_SAFKA_API_KEY', '').strip()
    if not key or os.getenv('SHOPY_SAFKA_READ_ENABLED') != '1':
        raise CatalogValidationError('Supplier shipping reads are disabled')
    with db() as conn:
        fresh = conn.execute('SELECT MIN(updated_at) AS stamp,COUNT(*) AS n FROM safka_shipping_rates').fetchone()
        if fresh['n'] and fresh['stamp']:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(fresh['stamp'])).total_seconds()
                if 0 <= age < SHIPPING_TTL:
                    return
            except (ValueError, TypeError):
                pass
        conn.execute("INSERT OR IGNORE INTO safka_schema_meta(key,value) VALUES('shipping_refresh_attempt','0')")
        previous = conn.execute("SELECT value FROM safka_schema_meta WHERE key='shipping_refresh_attempt'").fetchone()['value']
        now = time.time()
        if now - float(previous) < 60:
            raise CatalogValidationError('Supplier shipping refresh is pending')
        claimed = conn.execute("UPDATE safka_schema_meta SET value=? WHERE key='shipping_refresh_attempt' AND value=?",
                               (str(now), previous))
        if claimed.rowcount != 1:
            raise CatalogValidationError('Supplier shipping refresh is pending')
    def observed_read(*args,**kwargs):
        envelope,rows=read(*args,**kwargs)
        first=rows[0] if rows and isinstance(rows[0],dict) else {}
        cities=first.get('cities')
        first_city=cities[0] if isinstance(cities,list) and cities and isinstance(cities[0],dict) else {}
        # Structural diagnostics only: never credentials, customer data or full bodies.
        shape={'keys':sorted(envelope),'records':len(rows),'first_keys':sorted(first),
               'city_keys':sorted(first_city),'itemsCount':envelope.get('itemsCount'),
               'totalActiveGovernorates':envelope.get('totalActiveGovernorates')}
        with db() as conn:
            conn.execute("INSERT INTO safka_schema_meta(key,value) VALUES('shipping_response_shape',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (json.dumps(shape),))
        return envelope,rows
    try:
        rows = shipping_snapshot(key, read=observed_read)
    except (ValueError,OSError,TypeError,KeyError) as exc:
        reason=type(exc).__name__
        if isinstance(exc,CatalogValidationError):reason+=': '+str(exc)
        elif hasattr(exc,'code'):reason+=': HTTP '+str(exc.code)
        with db() as conn:
            conn.execute("INSERT INTO safka_schema_meta(key,value) VALUES('shipping_refresh_error',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (reason[:300],))
        raise
    with db() as conn:
        sync_shipping_snapshot(conn, rows)
        conn.execute("DELETE FROM safka_schema_meta WHERE key='shipping_refresh_error'")

