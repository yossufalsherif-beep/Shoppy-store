"""Safe read-only Safka API diagnostic: print FIELD NAMES, never secret/data values.

This diagnoses actual provider response keys so the Safka product/shipping mapping
can be verified before importing; does not send customer orders or write any DB.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from dotenv import load_dotenv
from safka_import import _extract_list, _SAFE_OPENER

load_dotenv(Path(__file__).resolve().parent / '.env')


def safe_keys(path, key, timeout=20):
    if not path.startswith('/api/v1/public/') or '?' in path or '#' in path or '//' in path:
        raise ValueError('Invalid Safka public read endpoint path')
    query = urllib.parse.urlencode({'page': 1, 'size': 2})
    req = urllib.request.Request('https://api.safka-eg.com' + path + '?' + query,
                                 headers={'api-safka-key':key,'Accept':'application/json','User-Agent':'ShopyStoreLocalIntegration/1.0'},
                                 method='GET')
    with _SAFE_OPENER.open(req, timeout=timeout) as response:
        payload = json.load(response)
    if isinstance(payload,dict) and payload.get('success') is False:
        raise ValueError('Safka reported an error; verify the account and endpoint')
    if not isinstance(payload,(list,dict)):
        raise ValueError('Unexpected Safka payload type')
    top_keys = sorted(payload.keys()) if isinstance(payload,dict) else ['<root-list>']
    try:
        records = _extract_list(payload, 'products' if 'product' in path else 'shipping')
    except ValueError:
        records = []
    first_keys = sorted(records[0].keys()) if records and isinstance(records[0],dict) else []
    return {'top_level_fields':top_keys,'record_field_names':first_keys,
            'sample_record_count':len(records),'values_and_secrets_shown':False}


def main():
    if os.getenv('SHOPY_SAFKA_READ_ENABLED') != '1':
        print('READ ONLY disabled: set SHOPY_SAFKA_READ_ENABLED=1 in local .env')
        return 2
    key=os.getenv('SHOPY_SAFKA_API_KEY','')
    if not key:
        print('Missing private SHOPY_SAFKA_API_KEY in your LOCAL .env')
        return 2
    for name, var in [('المنتجات','SHOPY_SAFKA_PRODUCTS_PATH'),('الشحن','SHOPY_SAFKA_SHIPPING_PATH')]:
        path=os.getenv(var,'').strip()
        if not path:
            print(name+': exact API path not configured. See official Safka Public API docs.')
            continue
        try:
            print(name+': '+json.dumps(safe_keys(path,key),ensure_ascii=False))
        except (OSError,ValueError,urllib.error.HTTPError) as exc:
            # Do not print raw HTTP response or secret keys.
            print(name+': failed safely: '+type(exc).__name__)
            return 2
    print('READ-ONLY CHECK. Nothing sent to Safka orders. No DB writes.')
    return 0

if __name__=='__main__':sys.exit(main())
