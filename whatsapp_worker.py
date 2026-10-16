"""Explicit opt-in WhatsApp sender worker. No live requests by default.

Do not run with --authorize-real-send until the owner has configured their
approved Meta sender, recipients, approved template, and allowed activation.
"""
import argparse
import os
import time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')
from app import db, init
from shopy_notify import send_one_pending


def main():
    parser = argparse.ArgumentParser(description='Shopy Store official WhatsApp outbox worker')
    parser.add_argument('--authorize-real-send', action='store_true', help='Explicitly permit real API calls')
    parser.add_argument('--watch', action='store_true', help='Keep checking pending notifications')
    parser.add_argument('--interval', type=int, default=20, help='Seconds between checks, minimum 10')
    args = parser.parse_args()
    if not args.authorize_real_send:
        print('LIVE SEND DISABLED. No messages sent. Add --authorize-real-send only after approval.')
        return
    if os.getenv('SHOPY_WA_ENABLED') != '1':
        print('LIVE SEND DISABLED: SHOPY_WA_ENABLED is not 1. No messages sent.')
        return
    if args.interval < 10:
        parser.error('--interval must be at least 10 seconds')
    init()
    print('Real WhatsApp sending explicitly enabled. Press Ctrl+C to stop.')
    try:
        while True:
            with db() as conn:
                state = send_one_pending(conn, authorized=True)
            # Print only status, never private recipient numbers or API secrets.
            print('Notification event:', state)
            if args.watch:
                time.sleep(args.interval if state in ('no_pending', 'disabled', 'busy') else 1)
            else:
                break
    except KeyboardInterrupt:
        print('Worker stopped by owner.')


if __name__ == '__main__':
    main()
