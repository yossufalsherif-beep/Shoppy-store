"""Optional long-running read-only Safka synchronizer.

Stop with Ctrl+C. It avoids hiding absent supplier items on unverified pagination. It never creates or sends supplier orders and requires a PRIVATE
key and verified official endpoint paths in local .env. Without these it refuses to run.
"""
import os
import sys
import time
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
from safka_import import main as sync

ROOT=Path(__file__).parent
load_dotenv(ROOT/'.env')


def run():
    if os.environ.get('SHOPY_SAFKA_READ_ENABLED')!='1':
        raise SystemExit('Safka API reads are OFF. Set SHOPY_SAFKA_READ_ENABLED=1 privately after authorization.')
    interval=int(os.environ.get('SHOPY_SAFKA_SYNC_MINUTES','15'))
    if interval < 5 or interval > 1440:
        raise SystemExit('Sync interval must be 5..1440 minutes')
    if not os.environ.get('SHOPY_SAFKA_API_KEY'):
        raise SystemExit('Missing SHOPY_SAFKA_API_KEY. Never share the secret in chat.')
    print('Starting Safka read-only catalog/stock/shipping sync every', interval, 'minutes.')
    print('No customer order submissions will occur in this process.')
    while True:
        try:
            print(datetime.now().isoformat(timespec='seconds'),'Starting sync')
            sync(['--from-api','--apply'])
        except Exception as e:
            print('Sync failed safely:',type(e).__name__,str(e)[:200],file=sys.stderr)
        time.sleep(interval*60)

if __name__=='__main__':
    try:run()
    except KeyboardInterrupt:print('Sync stopped')
