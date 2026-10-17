"""Restricted Safka callback endpoints and read-only product verification.

Webhook URL tokens are derived from server SECRET_KEY and are not API credentials.
Supplier callbacks cannot directly alter products, orders, or credentials.
"""
import hashlib
import hmac
import json
import urllib.parse
import urllib.request
from safka_bridge import CatalogValidationError, normalize_product
from safka_import import _SAFE_OPENER

def webhook_token(secret, kind):
    if kind not in ('product','order','callback'):
        raise ValueError('Unknown webhook')
    return hmac.new(secret.encode(),('shopy/safka/hook/v1/'+kind).encode(),hashlib.sha256).hexdigest()

def authenticate_hook(secret,kind,provided):
    return bool(provided) and hmac.compare_digest(webhook_token(secret,kind),provided)

def valid_callback_key(payload, configured_key):
    if not isinstance(payload,dict) or len(configured_key)<12:
        return False
    offered=payload.get('api_safka_key')
    return isinstance(offered,str) and hmac.compare_digest(offered,configured_key)

def product_identifier(payload):
    if not isinstance(payload,dict):
        return None
    # Event schemas are not published. Accept ID only; never trust stock in a callback.
    raw=payload.get('productId',payload.get('product_id'))
    if raw is None and isinstance(payload.get('product'),dict):
        raw=payload['product'].get('_id',payload['product'].get('id'))
    if not isinstance(raw,(int,str)) or isinstance(raw,bool):
        return None
    value=str(raw).strip()
    return value if 1<=len(value)<=100 and all(c.isalnum() or c in '_-' for c in value) else None

def fetch_product_detail(key,product_id,timeout=6):
    if not key or not product_id or not all(c.isalnum() or c in '_-' for c in product_id):
        raise CatalogValidationError('Invalid provider product lookup')
    url='https://api.safka-eg.com/api/v1/public/product/'+urllib.parse.quote(product_id,safe='')
    req=urllib.request.Request(url,headers={'api-safka-key':key,'Accept':'application/json'},method='GET')
    with _SAFE_OPENER.open(req,timeout=timeout) as response:
        body=response.read(512001)
    if len(body)>512000:
        raise CatalogValidationError('Product detail too large')
    envelope=json.loads(body)
    if not isinstance(envelope,dict) or envelope.get('success') is False:
        raise CatalogValidationError('Invalid product detail response')
    # Official Product By ID wraps the record in `product`, unlike list APIs.
    record=envelope.get('product',envelope.get('data',envelope))
    if not isinstance(record,dict):
        raise CatalogValidationError('Invalid detail product shape')
    item=normalize_product(record)
    if item['supplier_id'] != product_id:
        raise CatalogValidationError('Supplier product identity mismatch')
    return item,record

def verified_availability(item,raw):
    """Unknown stock remains unknown, even if Safka marks listing active."""
    explicit=raw.get('is_active')
    if explicit is False:
        return False
    if explicit is not None and explicit is not True:
        raise CatalogValidationError('Invalid supplier is_active value')
    if item['stock'] is None:
        return None
    return item['stock']>0 and explicit is not False
