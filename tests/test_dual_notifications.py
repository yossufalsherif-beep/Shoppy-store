"""Offline tests. Neither a Meta token nor a live network connection is required."""
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from shopy_notify import (
    init_notification_store, configured_recipients, validate_eg_mobile,
    queue_new_order, send_one_pending, build_template_parameters,
)


def sample():
    return {
        'order_ref':'SH-DEMO-007', 'customer_name':'Test Buyer',
        'customer_phone':'01012345678', 'governorate':'الجيزة',
        'city':'الدقي', 'area':'المهندسين',
        'products_total_piasters':12500, 'shipping_piasters':5000,
        'total_piasters':17500, 'payment_method':'الدفع عند الاستلام',
        'items':[{'name':'منتج تجريبي', 'qty':1, 'options':{'اللون':'أزرق'}}],
    }

class TestDualNotifications(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        init_notification_store(self.db)
        self.destinations = ['01012345678', '01123456789']
    def tearDown(self):
        self.db.close()

    def test_both_eg_numbers_and_duplicate_list(self):
        self.assertEqual(configured_recipients(','.join(self.destinations)),
                         ['201012345678', '201123456789'])
        self.assertEqual(configured_recipients('01012345678,201012345678'), ['201012345678'])
        with self.assertRaises(ValueError): validate_eg_mobile('0104351876')

    def test_two_events_have_equal_message_parameters(self):
        count = queue_new_order(self.db, sample(), recipients=self.destinations, enabled=True)
        self.assertEqual(count, 2)
        rows = self.db.execute('SELECT event_key,recipient,template_params_json,status FROM notification_outbox ORDER BY id').fetchall()
        self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertEqual(rows[0][2], rows[1][2])
        self.assertEqual(rows[0][3], 'pending')
        self.assertEqual(rows[1][3], 'pending')
        self.assertEqual(json.loads(rows[0][2]),build_template_parameters(sample()))
        queue_new_order(self.db, sample(), recipients=self.destinations, enabled=True)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0],2)

    def test_disabled_does_not_send_even_with_an_authorized_worker(self):
        queue_new_order(self.db,sample(),recipients=self.destinations,enabled=False)
        with patch.dict(os.environ,{'SHOPY_WA_ENABLED':'0'},clear=False):
            self.assertEqual(send_one_pending(self.db,authorized=True), 'disabled')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notification_outbox WHERE status="disabled"').fetchone()[0],2)

    def test_no_recipients_means_zero_events(self):
        self.assertEqual(queue_new_order(self.db,sample(),recipients=[],enabled=True),0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0],0)

    def test_two_separate_mock_acceptances(self):
        queue_new_order(self.db,sample(),recipients=self.destinations,enabled=True)
        captured=[]
        def fake_post(url, data, headers):
            payload=json.loads(data)
            captured.append(payload)
            return {'messages':[{'id':'wamid.TEST_'+str(len(captured))}]}
        env={'SHOPY_WA_ENABLED':'1','SHOPY_WA_ACCESS_TOKEN':'local_fake_token',
             'SHOPY_WA_PHONE_NUMBER_ID':'1234567890','SHOPY_WA_GRAPH_VERSION':'v24.0'}
        with patch.dict(os.environ,env,clear=False):
            self.assertEqual(send_one_pending(self.db,authorized=True,post=fake_post),'accepted')
            self.assertEqual(send_one_pending(self.db,authorized=True,post=fake_post),'accepted')
            self.assertEqual(send_one_pending(self.db,authorized=True,post=fake_post),'no_pending')
        self.assertEqual([p['to'] for p in captured],['201012345678','201123456789'])
        self.assertEqual(captured[0]['template'],captured[1]['template'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notification_outbox WHERE status="accepted"').fetchone()[0],2)

    def test_failed_provider_unknown_and_not_retried_automatically(self):
        queue_new_order(self.db,sample(),recipients=self.destinations,enabled=True)
        def unknown_post(*args): raise TimeoutError('fake-timeout')
        env={'SHOPY_WA_ENABLED':'1','SHOPY_WA_ACCESS_TOKEN':'local_fake_token',
             'SHOPY_WA_PHONE_NUMBER_ID':'1234567890','SHOPY_WA_GRAPH_VERSION':'v24.0'}
        with patch.dict(os.environ,env,clear=False):
            self.assertEqual(send_one_pending(self.db,authorized=True,post=unknown_post),'unknown')
            self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notification_outbox WHERE status="unknown"').fetchone()[0],1)

if __name__=='__main__':unittest.main()
