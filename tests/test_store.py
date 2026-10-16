"""Run locally after pip install -r requirements.txt: py -m unittest discover -s tests -v"""
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as store
from werkzeug.security import generate_password_hash

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dbname = str(Path(self.temp.name) / 'test.sqlite3')
        self.database_patch = patch.object(store, 'DB', self.dbname)
        self.database_patch.start()
        store.init()
        store.app.config.update(TESTING=True, SECRET_KEY='for-tests-only')
        self.client = store.app.test_client()
        with store.db() as conn:
            conn.execute("INSERT INTO users(email,password,role) VALUES(?,?,?)", ('admin@example.test', generate_password_hash('strong-owner-password'), 'admin'))
            # Explicit TEST fixtures; production initialization must never create demo records.
            conn.executemany('INSERT INTO shipping(governorate,fee) VALUES(?,?)',[('القاهرة',6000),('الجيزة',6000),('الإسكندرية',8000)])
            conn.executemany('INSERT INTO products(title,slug,description,price) VALUES (?,?,?,?)',[('منتج تجريبي أول','sample-one','منتج للاختبار',15000),('منتج تجريبي ثاني','sample-two','منتج للاختبار',23000)])
    def tearDown(self):
        self.database_patch.stop()
        self.temp.cleanup()
    def token(self):
        with self.client.session_transaction() as sess:
            sess['csrf'] = 'csrf-test-token'
        return 'csrf-test-token'
    def admin_login(self):
        self.client.post('/login', data={'email': 'admin@example.test', 'password': 'strong-owner-password','csrf':self.token()})
    def test_public_home_and_guest_browse_no_login(self):
        response = self.client.get('/', follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn('تصفح المنتجات'.encode('utf-8'), response.data)
        login = self.client.get('/login')
        self.assertEqual(login.status_code, 200)
        self.assertIn('تصفح المتجر بدون تسجيل دخول'.encode('utf-8'), login.data)
        self.assertEqual(self.client.get('/cart').status_code, 200)
    def test_catalog_product_and_sitemap(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/products/sample-one').status_code, 200)
        self.assertIn(b'/products/sample-one', self.client.get('/sitemap.xml').data)
    def test_customer_cannot_access_admin(self):
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.client.post('/register', data={'csrf':self.token(),'email':'customer@example.test','password':'customer-password-2026'})
        self.assertEqual(self.client.get('/admin').status_code, 403)
    def test_admin_product_with_options(self):
        self.admin_login()
        response=self.client.post('/admin/product', data={'csrf':self.token(),'title':'Cotton Tee','slug':'cotton-tee','price':'190','description':'soft','sizes':'M,L','colors':'أبيض,أسود'},follow_redirects=True)
        self.assertEqual(response.status_code,200)
        with store.db() as conn:
            row=conn.execute("SELECT * FROM products WHERE slug='cotton-tee'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['price'],19000)
        self.assertIn('المقاس',row['options_json'])
    def test_cart_variant_checkout_and_order_snapshot(self):
        self.admin_login()
        self.client.post('/admin/product',data={'csrf':self.token(),'title':'Tee','slug':'tee','price':'100','sizes':'M,L','colors':'أسود,أبيض'})
        with store.db() as conn:
            p=conn.execute("SELECT * FROM products WHERE slug='tee'").fetchone()
        self.client.post('/cart/add/'+str(p['id']),data={'csrf':self.token(),'option_المقاس':'L','option_اللون':'أسود'})
        resp=self.client.get('/checkout')
        self.assertEqual(resp.status_code,200)
        checkout=self.client.post('/checkout',data={'csrf':self.token(),'name':'Test Buyer','phone':'01012345678','governorate':'القاهرة','city':'القاهرة','area':'مدينة نصر','address':'شارع عشرة عمارة 4'},follow_redirects=True)
        self.assertEqual(checkout.status_code,200)
        self.assertIn('طلبك اتسجل'.encode('utf-8'),checkout.data)
        with store.db() as conn:
            row=conn.execute('SELECT * FROM orders').fetchone()
            item=conn.execute('SELECT * FROM order_items').fetchone()
            self.assertEqual(row['shipping_fee'],6000)
            self.assertEqual(row['total'],16000)
            self.assertIn('أسود',item['variant_json'])
    def test_csrf_rejects_invalid_request(self):
        self.assertEqual(self.client.post('/cart/add/1', data={'csrf':'wrong'}).status_code,400)
    def test_no_checkout_without_cart(self):
        self.assertEqual(self.client.get('/checkout').status_code,302)
    def test_migration_preserves_orders(self):
        with store.db() as c:
            c.execute("INSERT INTO orders(reference,customer,phone,governorate,address,shipping_fee,total) VALUES('SH-LEGACY','Legacy Buyer','01012345678','الجيزة','عنوان طويل في الجيزة',6000,16000)")
        store.init()
        with store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM orders WHERE reference='SH-LEGACY'").fetchone()[0],1)
            self.assertIn('options_json',[r['name'] for r in c.execute('PRAGMA table_info(products)')])

if __name__=='__main__':unittest.main()
