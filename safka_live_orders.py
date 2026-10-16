"""Owner-initiated Safka order submission.

No Safka Create Order contract is asserted by this module. The actual request
contract must be copied from the provider's published Create Order docs into a
private, audited JSON mapping before ANY real requests become possible.

This deliberately does not provide automatic checkout forwarding yet.
"""
from __future__ import annotations

import json
import os
import re
import ssl
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from safka_dry_run import prepare_dry_run
from safka_bridge import CatalogValidationError


class OrderContractError(ValueError):
    pass


class OrderSubmissionOutcome:
    def __init__(self, state, provider_order_id=None):
        self.state = state
        self.provider_order_id = provider_order_id


SOURCE_FIELDS = frozenset({
    'order_reference', 'customer_name', 'customer_phone', 'governorate',
    'address', 'customer_products_egp', 'customer_shipping_egp',
    'supplier_shipping_egp', 'total_to_collect_egp',
    'shipping_pricing_document_id', 'requested_commission_egp',
})
ITEM_FIELDS = frozenset({
    'supplier_product_id', 'supplier_product_code', 'quantity',
    'customer_unit_price_egp', 'supplier_unit_price_egp', 'choices', 'supplier_property_id',
})


def _as_egp(piastres):
    if not isinstance(piastres, int):
        raise OrderContractError('Missing monetary snapshot')
    return float(Decimal(piastres) / 100)


def canonical_order(preview, *, commission_egp=None, property_ids=None):
    """Local order, NOT the API payload. Separate shown shipping from Safka commission."""
    result = {
        'order_reference': preview['order_reference'],
        'customer_name': preview['customer_name'],
        'customer_phone': preview['customer_phone'],
        'governorate': preview['governorate'],
        'address': preview['address'],
        'customer_products_egp': _as_egp(preview['customer_products_piastres']),
        'customer_shipping_egp': _as_egp(preview['customer_shipping_piastres']),
        'supplier_shipping_egp': _as_egp(preview['estimated_supplier_shipping_piastres']),
        'total_to_collect_egp': _as_egp(preview['total_to_collect_piastres']),
    }
    result['shipping_pricing_document_id'] = preview.get('shipping_pricing_document_id')
    result['requested_commission_egp'] = commission_egp
    property_ids = property_ids or {}
    result['items'] = [{
        'supplier_product_id': i['supplier_product_id'],
        'supplier_product_code': i.get('supplier_product_code'),
        'quantity': i['quantity'],
        'customer_unit_price_egp': _as_egp(i['customer_unit_price_piastres']),
        'supplier_unit_price_egp': _as_egp(i['supplier_unit_price_piastres']),
        'choices': i['choices'],
        'supplier_property_id': property_ids.get(index),
    } for index, i in enumerate(preview['items'])]
    # Safka commission is NOT inferred from a shipping difference or a generic margin.
    return result


def _check_keys(mapping, allowed):
    if not isinstance(mapping, dict) or not mapping:
        raise OrderContractError('Verified contract must define a non-empty field map')
    for dest, source in mapping.items():
        if not isinstance(dest, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', dest):
            raise OrderContractError('Invalid provider field in contract')
        if not isinstance(source, str) or source not in allowed:
            raise OrderContractError('Unsupported source mapping: ' + str(source))


def validate_contract(contract):
    if not isinstance(contract, dict) or contract.get('schema_verified') is not True:
        raise OrderContractError('A provider-verified Create Order JSON contract is required')
    path = contract.get('path')
    if not isinstance(path, str) or not re.fullmatch(r'/api/v1/public/[A-Za-z0-9_/-]{1,160}', path):
        raise OrderContractError('Provider POST path must be verified under /api/v1/public/')
    if '..' in path or '//' in path:
        raise OrderContractError('Unsafe provider path')
    _check_keys(contract.get('fields'), SOURCE_FIELDS)
    _check_keys(contract.get('item_fields'), ITEM_FIELDS)
    items_name = contract.get('items_field')
    if not isinstance(items_name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', items_name):
        raise OrderContractError('Missing verified item list field')
    if items_name in contract['fields']:
        raise OrderContractError('Items field conflicts with another field')
    required_sources = {'customer_name','customer_phone','address','total_to_collect_egp'}
    if ('governorate' not in contract['fields'].values() and
        'shipping_pricing_document_id' not in contract['fields'].values()):
        raise OrderContractError('Missing supplier shipping-pricing document mapping')
    if not required_sources.issubset(set(contract['fields'].values())):
        raise OrderContractError('Missing minimum customer/total mapping')
    if not {'supplier_product_id','quantity'}.issubset(set(contract['item_fields'].values())):
        raise OrderContractError('Missing provider item identifiers or quantity')
    response_id_key = contract.get('response_order_id_path')
    if not isinstance(response_id_key, str) or not re.fullmatch(r'[A-Za-z_][\w]{0,49}(\.[A-Za-z_][\w]{0,49}){0,3}', response_id_key):
        raise OrderContractError('Missing known success-response order ID path')
    return contract


def load_contract(path=None):
    path = path or os.getenv('SHOPY_SAFKA_VERIFIED_CONTRACT_PATH', '') or str(Path(__file__).with_name('safka_documented_create_order.json'))
    if not path:
        raise OrderContractError('No verified Safka Create Order contract is configured')
    p = Path(path).resolve(strict=True)
    if not p.is_file() or p.stat().st_size > 20_000:
        raise OrderContractError('Contract file is missing or too large')
    return validate_contract(json.loads(p.read_text(encoding='utf8')))


def build_payload(canonical, contract):
    contract = validate_contract(contract)
    if not canonical['items']:
        raise OrderContractError('No supplier products')
    if any(item['choices'] for item in canonical['items']):
        # Safka option text must be mapped to a verified supplier variant ID.
        # Never place an order with unverified size/color selection.
        raise OrderContractError('Provider variant ID mapping is not verified; manual order review required')
    if ('supplier_product_code' in contract['item_fields'].values() and
        any(item['supplier_product_code'] is None for item in canonical['items'])):
        raise OrderContractError('Missing supplier item code')
    # Safka's published example requires a product _id and a property _id.
    if 'supplier_property_id' in contract['item_fields'].values():
        if any(not isinstance(item['supplier_property_id'], str) or not re.fullmatch(r'[A-Za-z0-9]{12,100}', item['supplier_property_id']) for item in canonical['items']):
            raise OrderContractError('Supplier property ID is required for each line item')
    if 'shipping_pricing_document_id' in contract['fields'].values():
        ident = canonical.get('shipping_pricing_document_id')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9]{12,100}', ident):
            raise OrderContractError('Missing verified price-list document ID for shipping')
    if 'requested_commission_egp' in contract['fields'].values():
        amount = canonical.get('requested_commission_egp')
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 <= amount <= canonical['total_to_collect_egp']:
            raise OrderContractError('Explicit numeric commission is required for this one-order test')
    fields = {target: canonical[source] for target, source in contract['fields'].items()}
    fields[contract['items_field']] = [
        {target: (str(item[source]) if source == 'quantity' and contract.get('stringify_quantity') else item[source])
         for target, source in contract['item_fields'].items()}
        for item in canonical['items']
    ]
    return fields


def _response_id(body, path):
    obj = body
    for component in path.split('.'):
        if not isinstance(obj, dict) or component not in obj:
            return None
        obj = obj[component]
    if isinstance(obj, (str, int)) and not isinstance(obj, bool) and str(obj).strip() and len(str(obj)) <= 150:
        return str(obj)
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit_payload(canonical, contract, api_key, *, opener=None, timeout=12):
    """Perform exactly one POST, only to approved Safka host. No automatic retries.

    Caller must have saved outbox claim *before* invocation. Unknown responses
    stay UNKNOWN; do not blindly retry a potentially accepted purchase.
    """
    validate_contract(contract)
    if not isinstance(api_key, str) or len(api_key.strip()) < 12:
        raise OrderContractError('API key is missing')
    payload = build_payload(canonical, contract)
    url = 'https://api.safka-eg.com' + contract['path']
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'api.safka-eg.com' or parsed.port is not None:
        raise OrderContractError('Invalid fixed supplier API host')
    if opener is None:
        opener = build_opener(NoRedirect(), HTTPSHandler(context=ssl.create_default_context()))
    req = Request(url, data=json.dumps(payload, ensure_ascii=False).encode('utf8'), method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'api-safka-key': api_key.strip(),
    })
    try:
        with opener.open(req, timeout=timeout) as response:
            raw = response.read(64_000)
            if int(response.status) not in (200, 201, 202):
                return OrderSubmissionOutcome('unknown')
    except HTTPError as exc:
        # Provider may still have processed a request, particularly on 409/5xx.
        return OrderSubmissionOutcome('rejected' if 400 <= exc.code < 409 or 410 <= exc.code < 500 else 'unknown')
    except (URLError, TimeoutError, OSError):
        return OrderSubmissionOutcome('unknown')
    try:
        response = json.loads(raw)
    except (ValueError, TypeError):
        return OrderSubmissionOutcome('unknown')
    if not isinstance(response, dict) or response.get('success') is False:
        return OrderSubmissionOutcome('unknown')
    provider_id = _response_id(response, contract['response_order_id_path'])
    if not provider_id:
        return OrderSubmissionOutcome('unknown')
    return OrderSubmissionOutcome('accepted', provider_id)


def claim_one_test_order(conn, order_id, expected_reference, submitted_commission_piastres=None):
    """Atomic claim. Existing DB status is the idempotency lock."""
    cur = conn.execute('''UPDATE safka_order_outbox SET status='test_sending',
      error='',attempt_count=attempt_count+1,updated_at=CURRENT_TIMESTAMP
      WHERE order_id=? AND status='awaiting_provider_configuration' ''', (order_id,))
    if cur.rowcount != 1:
        raise OrderContractError('This supplier order has already been claimed, submitted, or requires reconciliation')
    # Check order reference/status before claim was committed; caller owns transaction.
    ref = conn.execute('SELECT reference,status FROM orders WHERE id=?', (order_id,)).fetchone()
    if ref is None or ref['reference'] != expected_reference or ref['status'] == 'cancelled':
        raise OrderContractError('Reference mismatch or cancelled order')
    if submitted_commission_piastres is not None:
        if not isinstance(submitted_commission_piastres,int) or submitted_commission_piastres < 0:
            raise OrderContractError('Invalid submitted commission snapshot')
        conn.execute('''UPDATE safka_order_outbox SET submitted_commission_piastres=?
                      WHERE order_id=? AND status='test_sending' ''',(submitted_commission_piastres,order_id))


def record_outcome(conn, order_id, outcome):
    if outcome.state not in {'accepted', 'rejected', 'unknown'}:
        raise OrderContractError('Unexpected transport outcome')
    conn.execute('''UPDATE safka_order_outbox SET status=?,supplier_order_id=?,
      updated_at=CURRENT_TIMESTAMP WHERE order_id=? AND status='test_sending' ''',
      (outcome.state, outcome.provider_order_id, order_id))
