"""Import a vetted Safka catalog/price list into the existing Shopy SQLite database.

Use a local JSON export to verify field mappings first. Live API reads are optional,
and intentionally OFF without the owner's private API key and exact official paths.
No supplier orders are sent from this tool.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # NEVER forward private api-safka-key to a Location header.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

_SAFE_OPENER = urllib.request.build_opener(_NoRedirect())
from pathlib import Path
from dotenv import load_dotenv
from safka_bridge import init_safka_schema, import_products, import_shipping, normalize_product, normalize_shipping, CatalogValidationError

ROOT=Path(__file__).resolve().parent
load_dotenv(ROOT/'.env')


def _extract_list(obj, kind):
    if isinstance(obj, list):
        return obj
    if not isinstance(obj, dict):
        raise CatalogValidationError('Expected JSON list or an object wrapping it')
    for key in (kind, 'data', 'items', 'results'):
        data = obj.get(key)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for name in (kind,'items','products','prices','list'):
                if isinstance(data.get(name),list):
                    return data[name]
    raise CatalogValidationError(f'Could not identify {kind} array; inspect Safka actual response')


def load_json(path,kind):
    with open(path,encoding='utf-8') as f:
        return _extract_list(json.load(f),kind)


def _next_page(payload,page,amount,page_size):
    if not isinstance(payload,dict):
        return amount == page_size
    meta=payload.get('pagination') or payload.get('meta') or {}
    if not isinstance(meta,dict):
        meta={}
    # Stop only when a documented pagination flag / totalPages or a short page
    # indicates completion; a caller may have to adjust for a different API shape.
    for obj in (meta,payload):
        for key in ('has_next','hasNext','has_more','hasMore'):
            if key in obj:
                return bool(obj[key])
        for key in ('total_pages','totalPages','pages'):
            if key in obj:
                return page < int(obj[key])
    return amount == page_size


def read_safka_pages(api_key,relative_path,kind, size=50, max_pages=500):
    if not relative_path.startswith('/api/v1/public/') or '?' in relative_path or '#' in relative_path:
        raise CatalogValidationError('Configure an exact documented Safka public API path in .env')
    # Fixed trusted hostname, not a user-supplied URL: prevents SSRF/key leakage.
    base='https://api.safka-eg.com'
    all_rows=[]
    for page in range(1,max_pages+1):
        q=urllib.parse.urlencode({'page':page,'size':size})
        url=base+relative_path+'?'+q
        req=urllib.request.Request(url,headers={'api-safka-key':api_key,'Accept':'application/json','User-Agent':'ShopyStoreLocalIntegration/1.0'})
        try:
            with _SAFE_OPENER.open(req,timeout=20) as response:
                data=json.load(response)
        except urllib.error.HTTPError as e:
            raise CatalogValidationError(f'Safka HTTP {e.code}; check authorized account / endpoint') from None
        except (OSError,ValueError) as e:
            raise CatalogValidationError(f'Could not read Safka API: {type(e).__name__}') from None
        if isinstance(data,dict) and data.get('success') is False:
            raise CatalogValidationError('Safka indicated an API error; import halted')
        rows=_extract_list(data,kind)
        all_rows.extend(rows)
        if not _next_page(data,page,len(rows),size):
            return all_rows
    raise CatalogValidationError('Reached max pages: import halted to avoid partial full sync')


def main(argv=None):
    ap=argparse.ArgumentParser(description='Safe Safka -> Shopy product/shipping importer; NO order sends')
    ap.add_argument('--products-json',type=Path,help='Local JSON from documented products response')
    ap.add_argument('--shipping-json',type=Path,help='Local JSON from documented shipping price-list response')
    ap.add_argument('--from-api',action='store_true',help='Read official Safka using private key in .env and verified paths')
    ap.add_argument('--apply',action='store_true',help='Write after validation; default is preview only')
    ap.add_argument('--complete-catalog',action='store_true',help='Hide Safka products missing from a VERIFIED complete product snapshot')
    ap.add_argument('--db',default=os.environ.get('DATABASE_PATH',str(ROOT/'shopy.db')))
    ap.add_argument('--allow-demo-data',action='store_true',help='Explicitly permit importing bundled EXAMPLE fixtures for local testing')
    args=ap.parse_args(argv)
    if not args.products_json and not args.shipping_json and not args.from_api:
        ap.error('Pass --products-json, --shipping-json or --from-api')
    if args.from_api and (args.products_json or args.shipping_json):
        ap.error('Do not mix local fixture and API mode; preview each source separately')
    if args.from_api:
        if os.getenv('SHOPY_SAFKA_READ_ENABLED')!='1':
            ap.error('Read-only API sync disabled: SHOPY_SAFKA_READ_ENABLED is not 1')
        api_key=os.getenv('SHOPY_SAFKA_API_KEY','')
        if not api_key:
            ap.error('Set private SHOPY_SAFKA_API_KEY locally. Do NOT paste it in a chat.')
        pp=os.getenv('SHOPY_SAFKA_PRODUCTS_PATH','')
        sp=os.getenv('SHOPY_SAFKA_SHIPPING_PATH','')
        if not pp and not sp:
            ap.error('Set exact verified path(s) from the official API docs before reading')
        products=read_safka_pages(api_key,pp,'products') if pp else None
        shipping=read_safka_pages(api_key,sp,'shipping') if sp else None
        label='api'
    else:
        products=load_json(args.products_json,'products') if args.products_json else None
        shipping=load_json(args.shipping_json,'shipping') if args.shipping_json else None
        label='local-json'
    if products is not None:
        validated=[normalize_product(r) for r in products]
        if len(set(p['supplier_id'] for p in validated))!=len(validated):
            raise CatalogValidationError('Duplicate supplier IDs')
        print('Catalog validated:',len(validated),'items; stock unknown/zero stays hidden.')
        print('Sample mapping (no customer data):',json.dumps([
            {'id':x['supplier_id'],'supplier_EGP':x['supplier_cost']/100,
             'Shopy_EGP':x['price']/100,'stock':x['stock'],'category':x['category']} for x in validated[:3]
        ],ensure_ascii=False))
    if shipping is not None:
        validated_rates=[normalize_shipping(r) for r in shipping]
        print('Shipping rates validated:',len(validated_rates),'entries, +10 EGP to each base fee.')
    if args.apply and not args.allow_demo_data and (
        any('EXAMPLE_' in str(p.name).upper() for p in (args.products_json,args.shipping_json) if p)):
        ap.error('Refusing to publish EXAMPLE products without --allow-demo-data (local testing ONLY)')
    if not args.apply:
        print('DRY RUN ONLY. To save validated data use --apply. No external order requests.')
        return 0
    import sqlite3
    from app import init
    if args.db != os.environ.get('DATABASE_PATH',str(ROOT/'shopy.db')):
        os.environ['DATABASE_PATH']=args.db
        import app
        app.DB=args.db
    init()
    from app import db
    with db() as conn:
        init_safka_schema(conn)
        if products is not None:
            print('Catalog import:', import_products(conn,products,complete=args.complete_catalog,source_label=label))
        if shipping is not None:
            print('Shipping import:', import_shipping(conn,shipping,source_label=label))
    print('Saved safely. Existing manual products, orders and admin are untouched.')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except CatalogValidationError as e:
        print('IMPORT HALTED:',e,file=sys.stderr)
        sys.exit(2)
