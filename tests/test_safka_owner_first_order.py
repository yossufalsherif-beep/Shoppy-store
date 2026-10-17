"""Private first-order readiness tests. No Safka requests and no live customer data."""
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash
import app as store

class OwnerFirstOrderTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.dbpatch=patch.object(store,'DB',str(Path(self.folder.name)/'owner-first.sqlite3'))
        self.dbpatch.start()
        store.init()
        store.app.config.update(TESTING=True,SECRET_KEY='owner-first-tests')
        self.client=store.app.test_client()
        with store.db() as conn:
            conn.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',
              ('owner@example.test',generate_password_hash('test-password-long'),'admin'))
            conn.execute("""INSERT INTO products(
             title,slug,description,price,image,active,options_json,category,supplier_cost,
             source,supplier_product_id,supplier_available,supplier_listed,supplier_last_sync)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             ('حامل موبايل تجريبي','test-phone-stand','',10000,'',1,'{}',
             'إلكترونيات',7000,'safka','68ac8fcf189d18298df1ac88',1,1,
             datetime.now(timezone.utc).isoformat()))
            conn.execute("""INSERT INTO safka_shipping_rates(
              governorate,city,area,supplier_fee,fee,pricing_document_id,updated_at)
              VALUES(?,?,?,?,?,?,?)""",
              ('الجيزة','6 أكتوبر','',9000,10000,
               '675a00000000000000000003',datetime.now(timezone.utc).isoformat()))
    def tearDown(self):
        self.dbpatch.stop()
        self.folder.cleanup()
    def token(self):
        with self.client.session_transaction() as sess:
            sess['csrf']='test-csrf'
        return 'test-csrf'
    def owner_login(self):
        self.client.post('/login',data={'csrf':self.token(),'email':'owner@example.test',
                                        'password':'test-password-long'})
        return self.token()
    def draft_data(self):
        with store.db() as c:
            pid=c.execute("SELECT id FROM products WHERE source='safka' LIMIT 1").fetchone()[0]
        return dict(csrf=self.token(),action='prepare',product_id=str(pid),
                    name='Test Buyer',phone='01012345678',governorate='الجيزة',
                    city='6 أكتوبر',area='الحي السابع',
                    address='شارع اختباري عمارة خمسة شقة 2',draft_confirm='1')
    def test_requires_owner_session(self):
        self.assertEqual(self.client.get('/admin/safka/first-order').status_code,302)
        self.assertEqual(self.client.get('/admin/safka/first-order/quote?product_id=1').status_code,302)
    def test_one_click_draft_does_not_send_supplier_order(self):
        self.owner_login()
        quote=self.client.get('/admin/safka/first-order/quote',query_string={
          'product_id':self.draft_data()['product_id'],'governorate':'الجيزة',
          'city':'6 أكتوبر','area':'الحي السابع'})
        self.assertEqual(quote.status_code,200)
        self.assertEqual(quote.json['total_piastres'],20000)
        with patch('safka_live_orders.submit_payload',side_effect=AssertionError('MUST NOT SEND')):
            answer=self.client.post('/admin/safka/first-order',data=self.draft_data())
        self.assertEqual(answer.status_code,302)
        self.assertIn('/admin/safka/order/',answer.headers['Location'])
        with store.db() as c:
            order=c.execute('SELECT * FROM orders').fetchone()
            self.assertEqual(order['total'],20000)
            self.assertEqual(order['user_id'],1)
            self.assertEqual(order['safka_shipping_pricing_document_id'],'675a00000000000000000003')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM safka_order_outbox').fetchone()[0],1)
        preview=self.client.get(answer.headers['Location'])
        self.assertEqual(preview.status_code,200)
        self.assertIn('مراجعة الطلب'.encode(),preview.data)
    def test_missing_supplier_shipping_blocks_draft(self):
        self.owner_login()
        with store.db() as c:c.execute('DELETE FROM safka_shipping_rates')
        response=self.client.post('/admin/safka/first-order',data=self.draft_data())
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.headers['Location'],'/admin/safka/first-order')
        with store.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)

if __name__=='__main__':
    unittest.main()
