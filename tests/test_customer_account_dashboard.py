"""Google-first auth UI and accurate, isolated customer account dashboard tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash

import app as store


class CustomerDashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.patch=patch.object(store,'DB',str(Path(self.tmp.name)/'dashboard.sqlite3'))
        self.patch.start()
        store.init()
        store.app.config.update(TESTING=True,SECRET_KEY='test-account-dashboard')
        self.client=store.app.test_client()
        with store.db() as conn:
            conn.execute("""INSERT INTO products(title,slug,description,price,image,active,options_json,category)
                VALUES(?,?,?,?,?,?,?,?)""",('منتج تجريبي','account-test-product','وصف',10000,'',1,'{}','اختبار'))

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def token(self):
        with self.client.session_transaction() as sess:sess['csrf']='account-test-csrf'
        return 'account-test-csrf'

    def register(self,email='shopper@example.test'):
        response=self.client.post('/register',data={'csrf':self.token(),'email':email,
                       'password':'strong-customer-password','phone':'01012345678'})
        self.assertEqual(response.status_code,302)
        return response

    def test_google_is_only_social_button_and_guest_purchases_remain_available(self):
        for path in ('/login','/register'):
            with self.subTest(path=path):
                response=self.client.get(path)
                self.assertEqual(response.status_code,200)
                html=response.data.decode('utf-8')
                self.assertIn('action="/auth/google/start"',html)
                self.assertIn('class="google-signin"',html)
                self.assertIn('class="google-mark"',html)
                self.assertNotIn('action="/auth/facebook/start"',html)
                self.assertNotIn('social-facebook',html)
                self.assertIn('بدون',html)
        login=self.client.get('/login').data.decode('utf-8')
        self.assertNotIn('لمتابعة طلباتك وإتمام الشراء',login)

    def test_account_guest_redirect_and_real_customer_statistics(self):
        self.assertEqual(self.client.get('/account').status_code,302)
        self.register()
        token=self.token()
        page=self.client.get('/account')
        self.assertEqual(page.status_code,200)
        self.assertIn('نظرة عامة',page.data.decode())
        self.assertIn('طلباتي',page.data.decode())
        self.assertIn('بياناتي الشخصية',page.data.decode())
        self.assertIn('الأمان وإعدادات الحساب',page.data.decode())
        self.assertIn(b'01012345678',page.data)
        self.assertIn(b'shopper@example.test',page.data)
        self.assertNotIn('id="account-delete-dialog" open',page.data.decode())
        self.client.post('/favorites/toggle/1',data={'csrf':token})
        self.client.post('/products/1/review',data={'csrf':token,'rating':'5','comment':''})
        with store.db() as conn:
            uid=conn.execute("SELECT id FROM users WHERE email='shopper@example.test'").fetchone()[0]
            conn.execute("""INSERT INTO orders(reference,user_id,customer,phone,governorate,address,shipping_fee,total,status)
                VALUES(?,?,?,?,?,?,?,?,?)""",('SH-ACCOUNTTEST',uid,'Buyer','01012345678','الجيزة','شارع وهمي للاختبار',5000,15000,'confirmed'))
        html=self.client.get('/account').data.decode()
        self.assertIn('SH-ACCOUNTTEST',html)
        self.assertIn('account-test-product',html)
        self.assertIn('تقييماتك',html)
        self.assertIn('إجمالي الطلبات',html)
        self.assertIn('150.00',html)
        self.assertIn('01012345678',html)

    def test_google_linked_account_renders_only_real_connection(self):
        self.register()
        with store.db() as conn:
            uid=conn.execute("SELECT id FROM users WHERE email='shopper@example.test'").fetchone()[0]
            conn.execute("""INSERT INTO social_identities(provider,subject,user_id,auth_user_id)
                 VALUES(?,?,?,?)""",('google','known-test-subject',uid,'test-auth-uid'))
        html=self.client.get('/account').data.decode()
        self.assertIn('Google مرتبط بالحساب',html)
        self.assertIn('مرتبط ✓',html)
        self.assertNotIn('ربط Google</button>',html)

    def test_customer_cannot_view_another_customers_orders(self):
        self.register()
        with store.db() as conn:
            owner_id=conn.execute("SELECT id FROM users WHERE email='shopper@example.test'").fetchone()[0]
            other_id=conn.execute("INSERT INTO users(email,password,role) VALUES(?,?,?)",
              ('another@example.test',generate_password_hash('password-long-enough'),'customer')).lastrowid
            conn.execute("""INSERT INTO orders(reference,user_id,customer,phone,governorate,address,shipping_fee,total)
             VALUES(?,?,?,?,?,?,?,?)""",('SH-PRIVATE-ORDER',other_id,'Other Buyer','01112345678','الجيزة','عنوان خاص',6000,26000))
        page=self.client.get('/account')
        self.assertEqual(page.status_code,200)
        self.assertNotIn(b'SH-PRIVATE-ORDER',page.data)
        self.assertNotIn(b'01112345678',page.data)

    def test_admin_account_uses_owner_dashboard(self):
        with store.db() as conn:
            admin_id=conn.execute("INSERT INTO users(email,password,role) VALUES(?,?,?)",
               ('owner@example.test',generate_password_hash('owner-test-password'),'admin')).lastrowid
        with self.client.session_transaction() as sess:sess['uid']=admin_id
        response=self.client.get('/account')
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.location,'/admin')


if __name__=='__main__':
    unittest.main()
