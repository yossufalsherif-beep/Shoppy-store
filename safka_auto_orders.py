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

def active_product_property(raw, choices, quantity=1):
    """Only a single explicitly identified supplier property is unambiguous.
    Option/value mapping is supplier-specific; don't guess between variants.
    """
    values=raw.get('properties') if isinstance(raw,dict) else None
    if not isinstance(values,list) or not values:
        raise OrderContractError('Supplier product has no properties')
    values=[entry for entry in values if isinstance(entry,dict)
            and entry.get('is_available') is not False and entry.get('is_active') is not False
            and entry.get('value')!=0]
    if choices:
        # Only an exact supplier label match is accepted. Never choose a random variant.
        if len(choices)!=1:
            raise OrderContractError('Supplier option mapping is ambiguous')
        selected=next(iter(choices.values()))
        values=[entry for entry in values if isinstance(entry,dict) and entry.get('key')==selected]
    if len(values)!=1:
        raise OrderContractError('Supplier product needs an explicit property selection')
    entry=values[0]
    if not isinstance(entry,dict):
        raise OrderContractError('Invalid supplier property')
    pid=entry.get('_id')
    if not isinstance(pid,str) or not ID24.fullmatch(pid):
        raise OrderContractError('Missing verified 24-character supplier property ID')
    if entry.get('is_active') is False or entry.get('is_available') is False:
        raise OrderContractError('Supplier property is inactive')
    stock=entry.get('value')
    if stock is not None:
        if isinstance(stock,bool) or not isinstance(stock,(int,float)) or stock<quantity:
            raise OrderContractError('Supplier property has insufficient stock')
    return pid

def verify_checkout_products(rows, *, detail_fetch=None):
    """Verify supplier IDs, selected properties, quantities and costs before saving."""
    key=os.getenv('SHOPY_SAFKA_API_KEY','').strip()
    detail_fetch=detail_fetch or fetch_product_detail
    if len(rows)>50:
        raise OrderContractError('Too many supplier items for a single order')
    details={}
    requested={}
    for product,qty,choices,*_ in rows:
        ident=product['supplier_product_id']
        if not isinstance(ident,str) or not ID24.fullmatch(ident):
            raise OrderContractError('Invalid Safka product ID')
        if ident not in details:
            details[ident]=detail_fetch(key,ident)
        normalized,raw=details[ident]
        if normalized['supplier_id']!=ident or raw.get('is_active') is not True:
            raise OrderContractError('Supplier product unavailable')
        if normalized['supplier_cost']!=product['supplier_cost']:
            raise OrderContractError('Supplier price changed; refresh the cart')
        property_id=active_product_property(raw,choices,qty)
        selected=next(entry for entry in raw['properties'] if entry.get('_id')==property_id)
        if selected.get('sale_price') is not None:
            from safka_bridge import egp_to_piastres
            if egp_to_piastres(selected['sale_price'])!=product['supplier_cost']:
                raise OrderContractError('Selected supplier property price changed')
        requested[(ident,property_id)]=requested.get((ident,property_id),0)+qty
        active_product_property(raw,choices,requested[(ident,property_id)])
    return details

def merchant_commission(preview):
    """Reconcile the customer's COD total with supplier cost and delivery cost."""
    if os.getenv('SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED')!='1':
        raise OrderContractError('Supplier commission policy has not been verified')
    difference=(preview['total_to_collect_piastres']
                -preview['estimated_supplier_products_piastres']
                -preview['estimated_supplier_shipping_piastres'])
    if difference<0:
        raise OrderContractError('Selling below supplier price requires manual review')
    # Full merchant margin includes the configured +10 EGP shipping markup.
    return float(Decimal(difference)/Decimal(100))

def live_enabled():
    return (os.getenv('SHOPY_SAFKA_ORDER_PUSH_ACTIVE')=='1'
            and os.getenv('SHOPY_SAFKA_ORDER_SCHEMA_VERIFIED')=='1'
            and os.getenv('SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED')=='1'
            and os.getenv('SHOPY_PUBLIC_CHECKOUT_ENABLED')=='1'
            and bool(os.getenv('SHOPY_SAFKA_API_KEY','').strip()))

def submit_saved_order_once(db,order_id,*,detail_fetch=None, sender=None):
    """Use one durable claim before one POST; ambiguous supplier results stop retries.

    Caller invokes only after a local checkout transaction has committed.
    No automatic retry, even for timeouts or 5xx responses.
    """
    if not live_enabled():
        raise OrderContractError('Live supplier orders are disabled pending verification')
    detail_fetch=detail_fetch or fetch_product_detail
    sender=sender or submit_payload
    key=os.getenv('SHOPY_SAFKA_API_KEY','').strip()
    contract=load_contract()
    with db() as conn:
        preview=prepare_dry_run(conn,order_id)
    if len(preview['items'])>50:
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
        properties[idx]=active_product_property(raw,item.get('choices') or {},item['quantity'])
    commission=merchant_commission(preview)
    canonical=canonical_order(preview,commission_egp=commission,property_ids=properties)
    build_payload(canonical,contract)
    with db() as conn:
        claim_one_test_order(conn,order_id,preview['order_reference'],int(round(commission*100)))
    outcome=sender(canonical,contract,key)
    with db() as conn:record_outcome(conn,order_id,outcome)
    return outcome
