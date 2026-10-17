"""Private read-only order verification for Safka V4.2. ZERO network calls.

This is NOT a verified Safka Create Order JSON contract. It is the local
order information we need to map after checking actual provider docs/response.
"""
import argparse
import json
from pathlib import Path
import sqlite3
from safka_bridge import order_preview, CatalogValidationError, sale_price_piastres, SHIPPING_MARKUP_PIASTRES


def prepare_dry_run(conn, order_id):
    row = conn.execute("SELECT status FROM safka_order_outbox WHERE order_id=?", (order_id,)).fetchone()
    if not row:
        raise CatalogValidationError('This order was not staged as a Safka order')
    order_status = conn.execute('SELECT status FROM orders WHERE id=?', (order_id,)).fetchone()
    if order_status is None or order_status['status'] == 'cancelled':
        raise CatalogValidationError('Cancelled/unknown order cannot be prepared')
    raw = order_preview(conn, order_id)
    supplier_ship = raw['supplier_shipping_piastres']
    buyer_ship = raw['customer_shipping_piastres']
    if supplier_ship is None or buyer_ship != supplier_ship + SHIPPING_MARKUP_PIASTRES:
        raise CatalogValidationError('Cannot verify Safka shipping cost and +10 EGP margin')
    if not raw['items']:
        raise CatalogValidationError('Order has no Safka line items')
    items_total = 0
    supplier_items_total = 0
    for item in raw['items']:
        if not item['supplier_product_id'] or not isinstance(item['quantity'], int) or item['quantity'] < 1:
            raise CatalogValidationError('Missing Safka product code or invalid quantity')
        cost = item['supplier_unit_price_piastres']
        if not isinstance(cost, int) or cost <= 0:
            raise CatalogValidationError('Missing supplier cost snapshot: review manually')
        if item['customer_unit_price_piastres'] < cost:
            raise CatalogValidationError('Merchant price is below supplier cost: review before submission')
        items_total += item['customer_unit_price_piastres'] * item['quantity']
        supplier_items_total += cost * item['quantity']
    if items_total + buyer_ship != raw['customer_total_piastres']:
        raise CatalogValidationError('Customer order total does not match saved item/shipping snapshots')
    return {
        'mode': 'DRY_RUN_ONLY',
        'is_verified_safka_create_order_payload': False,
        'shipping_pricing_document_id':raw.get('shipping_pricing_document_id'),
        'supplier_city_id':raw.get('supplier_city_id'),
        'order_reference': raw['merchant_reference'],
        'customer_name': raw['customer'],
        'customer_phone': raw['phone'],
        'governorate': raw['governorate'],
        'address': raw['address'],
        'items': raw['items'],
        'customer_products_piastres': items_total,
        'customer_shipping_piastres': buyer_ship,
        'total_to_collect_piastres': raw['customer_total_piastres'],
        'estimated_supplier_products_piastres': supplier_items_total,
        'estimated_supplier_shipping_piastres': supplier_ship,
        'estimated_margin_before_costs_piastres': raw['customer_total_piastres'] - supplier_items_total - supplier_ship,
        'provider_submission': 'DISABLED_IN_V4_2',
    }


def public_safe_summary(full):
    """Redact buyer PII by default for command line logs and screen sharing."""
    return {key: val for key, val in full.items() if key not in ('customer_name','customer_phone','address') and key != 'items'} | {
        'items': [{'supplier_product_id': item['supplier_product_id'],
                   'supplier_product_code': item.get('supplier_product_code'),
                   'quantity': item['quantity'],
                   'customer_unit_price_piastres': item['customer_unit_price_piastres']}
                  for item in full['items']]
    }


def main():
    parser = argparse.ArgumentParser(description='Safka order LOCAL DRY-RUN (no network writes)')
    parser.add_argument('order_id', type=int, help='Internal Shopy order ID; not reference text')
    parser.add_argument('--db', default=str(Path(__file__).resolve().parent/'shopy.db'))
    parser.add_argument('--include-customer-data', action='store_true', help='Show PRIVATE PII locally; do not share output')
    args = parser.parse_args()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        value = prepare_dry_run(conn, args.order_id)
        print(json.dumps(value if args.include_customer_data else public_safe_summary(value), ensure_ascii=False, indent=2))
        print('NO REQUEST WAS SENT TO SAFKA')
    finally:
        conn.close()

if __name__ == '__main__':
    try:
        main()
    except CatalogValidationError as err:
        print('DRY RUN BLOCKED:', err)
        raise SystemExit(2)
