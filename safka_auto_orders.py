"""Safe, idempotent Safka supplier order dispatch.

Server-side only. Does not send unless caller explicitly enabled the reviewed
Safka contract + commission interpretation + real checkout in Vercel.
"""
import os
import re
from decimal import Decimal
from safka_bridge import CatalogValidationError
from safka_dry_run import prepare_dry_run
from safka_live_orders import (
    OrderContractError,canonical_order,load_contract,build_payload,
    claim_one_test_order,submit_payload,record_outcome
)
from safka_webhooks import fetch_product_detail

ID24=re.compile(r'^[a-fA-F0-9]{24}$')

def active_product_property(raw, choices):
    """Only a single explicitly identified supplier property is unambiguous.
    Option/value mapping is supplier-specific; don't guess between variants.
    """
    if choices:
        raise OrderContractError('Cannot send selected variants without verified Safka property mapping')
    values=raw.get('properties') if isinstance(raw,dict) else None
    if not isinstance(values,list) or len(values)!=1:
        raise OrderContractError('Supplier product needs one verified property ID')
    entry=values[0]
    if not isinstance(entry,dict):
        raise OrderContractError('Invalid supplier property')
    pid=entry.get('_id')
    if not isinstance(pid,str) or not ID24.fullmatch(pid):
        raise OrderContractError('Missing verified 24-character supplier property ID')
    if entry.get('is_active') is False:
        raise OrderContractError('Supplier property is inactive')
    return pid

def merchant_commission(preview):
    """Financial policy MUST be verified with Safka on a one-order test.
    Never include the markup shipping unless the policy has been confirmed.
    """
    if os.getenv('SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED')!='1':
        raise OrderContractError('Supplier commission policy has not been verified')
    difference=preview['customer_products_piastres']-preview['estimated_supplier_products_piastres']
    if difference<0:
        raise OrderContractError('Selling below supplier price requires manual review')
    # Shipping +10 remains separate from Safka commission.
    return float(Decimal(difference)/Decimal(100))

def live_enabled():
    return (os.getenv('SHOPY_SAFKA_ORDER_PUSH_ACTIVE')=='1'
            and os.getenv('SHOPY_SAFKA_ORDER_SCHEMA_VERIFIED')=='1'
            and os.getenv('SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED')=='1'
            and os.getenv('SHOPY_PUBLIC_CHECKOUT_ENABLED')=='1'
            and bool(os.getenv('SHOPY_SAFKA_API_KEY','').strip()))

def submit_saved_order_once(db,order_id,*,detail_fetch=fetch_product_detail,
                            sender=submit_payload):
    """Use one durable claim before one POST; ambiguous supplier results stop retries.

    Caller invokes only after a local checkout transaction has committed.
    No automatic retry, even for timeouts or 5xx responses.
    """
    if not live_enabled():
        raise OrderContractError('Live supplier orders are disabled pending verification')
    key=os.getenv('SHOPY_SAFKA_API_KEY','').strip()
    contract=load_contract()
    with db() as conn:
        preview=prepare_dry_run(conn,order_id)
    if len(preview['items'])>8:
        raise OrderContractError('Too many supplier items for a single order')
    properties={}
    for idx,item in enumerate(preview['items']):
        product_id=item['supplier_product_id']
        if not isinstance(product_id,str) or not ID24.fullmatch(product_id):
            raise OrderContractError('Invalid Safka product ID')
        verified,raw=detail_fetch(key,product_id)
        if verified['supplier_id'] != product_id or raw.get('is_active') is False:
            raise OrderContractError('Supplier product unavailable')
        if verified['supplier_cost']!=item['supplier_unit_price_piastres']:
            raise OrderContractError('Supplier price changed since local order')
        properties[idx]=active_product_property(raw,item.get('choices') or {})
    commission=merchant_commission(preview)
    canonical=canonical_order(preview,commission_egp=commission,property_ids=properties)
    build_payload(canonical,contract)
    with db() as conn:
        claim_one_test_order(conn,order_id,preview['order_reference'],int(round(commission*100)))
    outcome=sender(canonical,contract,key)
    with db() as conn:record_outcome(conn,order_id,outcome)
    return outcome
