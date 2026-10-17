"""Run on Windows: py -m unittest discover -s tests -v"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as store
from werkzeug.security import generate_password_hash


class V4Tests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.patch=patch.object(store,'DB',str(Path(self.folder.name)/'tests.db'))
        self.patch.start()
        store.init()
        store.app.config.update(TESTING=True, SECRET_KEY='tests-v4-key')
        self.client=store.app.test_client()
        with store.db() as c:
            c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',
                      ('owner@shopy.test',generate_password_hash('long-strong-password'),'admin'))
            # Explicit TEST fixtures; production initialization must never create demo records.
            c.executemany('INSERT INTO shipping(governorate,fee) VALUES(?,?)',[('القاهرة',6000),('الجيزة',6000),('الإسكندرية',8000)])
            c.executemany('INSERT INTO products(title,slug,description,price) VALUES (?,?,?,?)',[('منتج تجريبي أول','sample-one','منتج للاختبار',15000),('منتج تجريبي ثاني','sample-two','منتج للاختبار',23000)])
    def tearDown(self):
        self.patch.stop();self.folder.cleanup()
    def token(self):
        with self.client.session_transaction() as s:s['csrf']='test-csrf'
        return 'test-csrf'
    def admin(self):
        self.client.post('/login',data={'csrf':self.token(),'email':'owner@shopy.test','password':'long-strong-password'})
    def test_public_browsing_and_assistant(self):
        for path in ('/','/products/sample-one','/assistant','/compare'):
            self.assertEqual(self.client.get(path).status_code,200,path)
        r=self.client.post('/assistant',data={'csrf':self.token(),'need':'منتج تجريبي','budget':'200'})
        self.assertEqual(r.status_code,200)
        self.assertIn('منتج تجريبي أول'.encode(),r.data)
    def test_compare_add_and_remove(self):
        r=self.client.post('/compare/add/1',data={'csrf':self.token()},follow_redirects=True)
        self.assertEqual(r.status_code,200)
        self.assertIn('منتج تجريبي أول'.encode(),self.client.get('/compare').data)
        self.client.post('/compare/remove/1',data={'csrf':self.token()})
        self.assertNotIn('منتج تجريبي أول'.encode(),self.client.get('/compare').data)
    def test_region_shipping_quote_and_checkout_server_price(self):
        self.admin()
        self.client.post('/admin/shipping-zone',data={'csrf':self.token(),'governorate':'القاهرة','city':'مدينة نصر','area':'الحي السابع','fee':'82','supplier_cost':'50'})
        r=self.client.get('/api/shipping-quote?governorate=القاهرة&city=مدينة نصر&area=الحي السابع')
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.get_json()['shipping_piastres'],8200)
        self.assertEqual(self.client.get('/api/shipping-quote?governorate=القاهرة&city=غير معروف').get_json()['shipping_piastres'],6000)
        self.client.post('/cart/add/1',data={'csrf':self.token()})
        response=self.client.post('/checkout',data={'csrf':self.token(),'name':'Buyer Name','phone':'01012345678','governorate':'القاهرة','city':'مدينة نصر','area':'الحي السابع','address':'العمارة ٤ الدور الخامس الشقة ٢'})
        self.assertEqual(response.status_code,302)
        with store.db() as c:
            row=c.execute('SELECT shipping_fee,shipping_supplier_cost,total FROM orders ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual(row['shipping_fee'],8200)
            self.assertEqual(row['shipping_supplier_cost'],5000)
            self.assertEqual(row['total'],23200)
    def test_product_category_and_private_cost(self):
        self.admin()
        response=self.client.post('/admin/product',data={'csrf':self.token(),'title':'Wireless Device','slug':'wireless-device','price':'350','supplier_cost':'200','category':'إلكترونيات'})
        self.assertEqual(response.status_code,302)
        with store.db() as c:
            row=c.execute("SELECT * FROM products WHERE slug='wireless-device'").fetchone()
            self.assertEqual(row['supplier_cost'],20000)
            self.assertEqual(row['category'],'إلكترونيات')
        self.assertNotIn('200.00'.encode(),self.client.get('/products/wireless-device').data)
    def test_public_contact_separate_from_private_notification(self):
        self.admin()
        self.client.post('/admin/contact',data={'csrf':self.token(),'support_phone':'01012345678','support_email':'help@shopy.test'})
        html=self.client.get('/').data.decode('utf-8')
        self.assertIn('help@shopy.test',html)
        self.assertIn('01012345678',html)
        self.assertNotIn('SHOPY_WA_RECIPIENTS',html)
    def test_payment_safka_disabled_and_admin_only(self):
        self.assertEqual(self.client.get('/admin/integrations').status_code,302)
        self.client.post('/register',data={'csrf':self.token(),'email':'visitor@shopy.test','password':'customer-strong-password'})
        self.assertEqual(self.client.get('/admin/integrations').status_code,403)
        html=self.client.get('/assistant').data.decode('utf-8')
        self.assertIn('مساعد',html)
    def test_utm_attribution_saved_not_publicly_leaked(self):
        self.client.get('/products/sample-one?utm_source=facebook&utm_campaign=fall_launch')
        self.client.post('/register',data={'csrf':self.token(),'email':'buyer-utm@shopy.test','password':'password-with-ten-chars'})
        self.client.post('/cart/add/1',data={'csrf':self.token()})
        self.client.post('/checkout',data={'csrf':self.token(),'name':'Test Buyer','phone':'01012345678','governorate':'القاهرة','city':'مدينة نصر','area':'حي','address':'عمارة خمسة وسبعين شارع التحرير'})
        with store.db() as c:
            o=c.execute('SELECT utm_source,utm_campaign FROM orders ORDER BY id DESC LIMIT 1').fetchone()
            self.assertEqual((o['utm_source'],o['utm_campaign']),('facebook','fall_launch'))
    def test_legacy_record_survives_multiple_migrations(self):
        with store.db() as c:
            c.execute("INSERT INTO orders(reference,customer,phone,governorate,address,shipping_fee,total) VALUES ('SH-OLD-TEST','Older Customer','01012345678','الجيزة','Old Address',5000,12000)")
        store.init();store.init()
        with store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM orders WHERE reference='SH-OLD-TEST'").fetchone()[0],1)

if __name__=='__main__':unittest.main()
