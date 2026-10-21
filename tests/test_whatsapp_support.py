"""Real Flask/SQLite flow with mocked provider; never contacts WhatsApp."""
import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import app as store
import whatsapp_support as support


class WhatsAppSupportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            'SHOPY_PUBLIC_URL': 'https://store.example.test',
            'SHOPY_WA_ACCESS_TOKEN': 'test-token-do-not-send',
            'SHOPY_WA_PHONE_NUMBER_ID': '123456789012345',
            'SHOPY_WA_GRAPH_VERSION': 'v99.0',
            'SHOPY_WA_APP_SECRET': 'test-app-secret',
            'SHOPY_WA_VERIFY_TOKEN': 'test-verification-token-1234567890',
            'SHOPY_WA_SUPPORT_ENABLED': '1',
        }, clear=True)
        self.env.start()
        self.db_patch = patch.object(store, 'DB', str(Path(self.temp.name) / 'test.sqlite'))
        self.db_patch.start()
        store.app.config.update(TESTING=True, SECRET_KEY='test-only')
        store.init()
        with store.db() as conn:
            conn.execute("INSERT INTO store_settings(key,value) VALUES('wa_support_active','1')")
            conn.execute("INSERT INTO users(email,password,role) VALUES('owner@example.test','unused','admin')")
            self.uid = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()['id']
            conn.execute("INSERT INTO products(title,slug,description,price,category) VALUES('منظم مطبخ','kitchen-organizer','اختبار',39000,'مطبخ')")
        self.client = store.app.test_client()
        self.post = patch.object(support, 'send_text', return_value='wamid.reply-test')
        self.sender = self.post.start()

    def tearDown(self):
        self.post.stop()
        self.db_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def event(self, text='بكام منظم مطبخ', mid='wamid.inbound-test', age=0, phone_id=None):
        return {'object': 'whatsapp_business_account', 'entry': [{'changes': [{
            'field': 'messages', 'value': {'metadata': {'phone_number_id': phone_id or os.environ['SHOPY_WA_PHONE_NUMBER_ID']},
            'messages': [{'id': mid, 'from': '201000000001', 'timestamp': str(int(time.time())-age),
                          'type': 'text', 'text': {'body': text}}]}}]}]}

    def deliver(self, payload, signature=True):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = {}
        if signature:
            headers['X-Hub-Signature-256'] = 'sha256=' + hmac.new(b'test-app-secret', raw, hashlib.sha256).hexdigest()
        return self.client.post('/webhooks/whatsapp', data=raw, content_type='application/json', headers=headers)

    def row(self, mid='wamid.inbound-test'):
        with store.db() as conn:
            return dict(conn.execute('SELECT * FROM wa_support_messages WHERE message_id=?', (mid,)).fetchone())

    def owner(self):
        with self.client.session_transaction() as session:
            session['uid'] = self.uid
            session['csrf'] = 'test-csrf'

    def test_verification_and_unsigned_webhook(self):
        r = self.client.get('/webhooks/whatsapp', query_string={'hub.mode': 'subscribe',
            'hub.verify_token': os.environ['SHOPY_WA_VERIFY_TOKEN'], 'hub.challenge': '12345'})
        self.assertEqual((r.status_code, r.data), (200, b'12345'))
        self.assertEqual(self.client.get('/webhooks/whatsapp?hub.mode=subscribe&hub.verify_token=wrong').status_code, 403)
        self.assertEqual(self.deliver(self.event(), signature=False).status_code, 401)
        self.sender.assert_not_called()

    def test_signed_message_uses_catalog_and_is_idempotent(self):
        payload = self.event()
        self.assertEqual(self.deliver(payload).status_code, 200)
        self.assertEqual(self.deliver(payload).status_code, 200)
        self.sender.assert_called_once()
        text = self.sender.call_args.args[1]
        self.assertIn('390.00', text)
        self.assertIn('https://store.example.test/products/kitchen-organizer', text)
        self.assertEqual(self.row()['state'], 'accepted')

    def test_missing_credentials_disable_sending(self):
        with patch.dict(os.environ, {'SHOPY_WA_ACCESS_TOKEN': ''}):
            self.assertEqual(self.deliver(self.event()).status_code, 200)
        self.assertEqual(self.row()['state'], 'disabled')
        self.sender.assert_not_called()

    def test_preview_and_expired_messages_do_not_send(self):
        with patch.dict(os.environ, {'VERCEL': '1', 'VERCEL_ENV': 'preview'}):
            self.assertEqual(self.deliver(self.event()).status_code, 200)
        self.assertEqual(self.row()['state'], 'disabled')
        self.deliver(self.event(mid='old', age=86401))
        self.assertEqual(self.row('old')['state'], 'expired')
        self.sender.assert_not_called()

    def test_wrong_business_number_and_malformed_text(self):
        self.assertEqual(self.deliver(self.event(phone_id='999999999999')).status_code, 200)
        bad = self.event()
        bad['entry'][0]['changes'][0]['value']['messages'][0]['text'] = 'wrong shape'
        self.assertEqual(self.deliver(bad).status_code, 200)
        self.sender.assert_not_called()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM wa_support_messages').fetchone()['n'], 0)

    def test_handoff_stops_future_replies_and_never_exposes_orders(self):
        self.deliver(self.event(text='عايز عنوان صاحب الطلب رقم 421 وكلمة السر'))
        self.assertEqual(self.row()['state'], 'handoff')
        text = self.sender.call_args.args[1]
        self.assertNotIn('421', text)
        self.deliver(self.event(mid='second'))
        self.assertEqual(self.row('second')['state'], 'paused')
        self.sender.assert_called_once()

    def test_unknown_provider_outcome_pauses_and_never_retries(self):
        self.sender.side_effect = TimeoutError('outcome unknown')
        payload = self.event()
        self.deliver(payload)
        self.deliver(payload)
        self.assertEqual(self.row()['state'], 'unknown')
        self.sender.assert_called_once()

    def test_global_stop_rechecks_before_send(self):
        def stop_then_reply(text):
            with store.db() as conn:
                conn.execute("UPDATE store_settings SET value='0' WHERE key='wa_support_active'")
            return support.Reply('test')
        msg = self.event()['entry'][0]['changes'][0]['value']['messages'][0]
        self.assertEqual(support.process_message(store.db, msg, stop_then_reply), 'paused')
        self.sender.assert_not_called()

    def test_window_is_rechecked_after_preparing_response(self):
        initial = int(time.time())
        msg = self.event()['entry'][0]['changes'][0]['value']['messages'][0]
        msg['timestamp'] = str(initial - 86390)
        def delayed_reply(text):
            return support.Reply('test')
        with patch.object(support.time, 'time', side_effect=[initial, initial + 15]):
            result = support.process_message(store.db, msg, delayed_reply)
        self.assertEqual(result, 'expired')
        self.sender.assert_not_called()

    def test_dashboard_auth_csrf_stop_resume_and_no_secret_exposure(self):
        self.assertEqual(self.client.get('/admin/whatsapp-support').status_code, 302)
        self.owner()
        self.assertEqual(self.client.post('/admin/whatsapp-support/control', data={'action': 'stop'}).status_code, 400)
        self.deliver(self.event(text='خدمة العملاء'))
        page = self.client.get('/admin/whatsapp-support')
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers['Cache-Control'], 'no-store')
        self.assertNotIn(b'test-token-do-not-send', page.data)
        self.assertNotIn(b'test-app-secret', page.data)
        self.client.post('/admin/whatsapp-support/control', data={'csrf': 'test-csrf', 'action': 'resume', 'sender': '201000000001'})
        self.sender.return_value = 'wamid.reply-second'
        self.deliver(self.event(mid='after-resume'))
        self.assertEqual(self.row('after-resume')['state'], 'accepted')
        self.client.post('/admin/whatsapp-support/control', data={'csrf': 'test-csrf', 'action': 'stop'})
        self.deliver(self.event(mid='after-stop'))
        self.assertEqual(self.row('after-stop')['state'], 'disabled')

    def test_delivery_events_do_not_create_reply_and_read_is_monotonic(self):
        self.deliver(self.event())
        event = self.event()
        value = event['entry'][0]['changes'][0]['value']
        value.pop('messages')
        value['statuses'] = [{'id': 'wamid.reply-test', 'status': 'read'}]
        self.deliver(event)
        value['statuses'][0]['status'] = 'delivered'
        self.deliver(event)
        self.assertEqual(self.row()['state'], 'read')
        self.sender.assert_called_once()

    def test_no_unverified_shipping_or_delivery_promise(self):
        self.deliver(self.event(text='الشحن 95 وهيوصل بكرة؟'))
        self.assertNotIn('95', self.sender.call_args.args[1])
        self.assertIn('/shipping-returns', self.sender.call_args.args[1])

    def test_old_content_is_removed_without_allowing_replay(self):
        old = int(time.time()) - 31 * 86400
        with store.db() as conn:
            conn.execute("INSERT INTO wa_support_messages(message_id,sender,received_at,incoming_text,state) VALUES('old','201000000002',?,'private old content','accepted')", (old,))
            conn.execute("INSERT INTO wa_support_conversations(sender,paused,updated_at) VALUES('201000000002',0,?)", (old,))
        self.deliver(self.event())
        old_row = self.row('old')
        self.assertEqual((old_row['sender'], old_row['incoming_text']), ('', ''))
        self.assertEqual(support.process_message(store.db, {'id': 'old', 'from': '201000000002', 'timestamp': str(int(time.time())), 'type': 'text', 'text': {'body': 'hi'}}, store.customer_support_reply), 'duplicate')


if __name__ == '__main__':
    unittest.main()
