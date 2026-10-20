"""Regression checks for safe customer deletion and relevant Arabic product search."""
import os
import time
import unittest
from unittest.mock import patch
from tests.test_store import StoreTests
from catalog_search import normalize,search_ranked,page_ranked
import app as store

class SmartSearchTests(unittest.TestCase):
    def setUp(self):
        self.rows=[
            {'id':1,'title':'مشط تصفيف الشعر الحراري','description':'للاستخدام اليومي','category':'العناية بالشعر','price':13000},
            {'id':2,'title':'زيت عطري','description':'منتج للاستخدام مع مشط تصفيف الشعر','category':'العطور','price':7000},
            {'id':3,'title':'مشط جيب صغير','description':'للحقيبة','category':'العناية بالشعر','price':8000},
            {'id':4,'title':'شاحن الهاتف','description':'ملحقات الهواتف','category':'إلكترونيات','price':14000},
        ]
    def test_title_first_not_description_noise(self):
        products=search_ranked(self.rows,'مشط تصفيف الشعر')
        self.assertEqual(products[0]['id'],1)
        self.assertNotIn(2,[p['id'] for p in products])
        self.assertNotIn(4,[p['id'] for p in products])
    def test_arabic_spelling_and_nonadjacent_terms(self):
        self.assertEqual(search_ranked(self.rows,'المشط الحراري')[0]['id'],1)
        self.assertEqual(search_ranked(self.rows,'تصفيف مشط')[0]['id'],1)
    def test_typo_support_with_no_unrelated_products(self):
        self.assertEqual(search_ranked(self.rows,'تصفيف الشعر')[0]['id'],1)
        self.assertEqual(search_ranked(self.rows,'مشطط تصفيف')[0]['id'],1)
        self.assertEqual(search_ranked(self.rows,'تليفزيون'),[])
    def test_colloquial_dahr_and_formal_zahr_produce_identical_results(self):
        products = self.rows + [
            {'id':5,'title':'حزام دعم الظهر الطبي','description':'مريح للظهر','category':'العناية بالجسم','price':21000},
            {'id':6,'title':'مسند ضهر للكرسي','description':'دعم الضهر','category':'مستلزمات المنزل','price':16000},
            {'id':7,'title':'شاشة عرض','description':'جهاز عرض الصور','category':'إلكترونيات','price':80000},
        ]
        for spelling_a,spelling_b in (
            ('ضهر','ظهر'),('الضهر','الظهر'),
            ('مسند ضهر','مسند ظهر'),
            ('حزام دعم الضهر','حزام دعم الظهر'),
            ('للضهر','للظهر'),
        ):
            with self.subTest(a=spelling_a,b=spelling_b):
                a = [item['id'] for item in search_ranked(products,spelling_a)]
                b = [item['id'] for item in search_ranked(products,spelling_b)]
                self.assertEqual(a,b)
        self.assertEqual([item['id'] for item in search_ranked(products,'ظهر')],[6,5])
        self.assertNotIn(7,[item['id'] for item in search_ranked(products,'ضهر')])

    def test_only_known_spelling_variant_is_folded_not_all_dad_za(self):
        for colloquial,formal in (
            ('ضهر','ظهر'),('الضهر','الظهر'),('بالضهر','بالظهر'),
            ('للضهر','للظهر'),('ضهري','ظهري'),('وجع الضهر','وجع الظهر'),
        ):
            with self.subTest(colloquial=colloquial):
                self.assertEqual(normalize(colloquial),normalize(formal))
        self.assertNotEqual(normalize('ظلم'),normalize('ضلم'))
        self.assertNotEqual(normalize('ضابط'),normalize('ظابط'))

    def test_price_sort_after_relevance_filter(self):
        products=search_ranked(self.rows,'مشط',sort='price_low')
        self.assertEqual([x['id'] for x in products],[3,1])
        self.assertEqual(page_ranked(products,'1',1)[1]['total'],2)

class CustomerDeletionTests(unittest.TestCase):
    token=StoreTests.token
    def setUp(self):
        StoreTests.setUp(self)
        self.client.post('/register',data={
            'csrf':self.token(),'email':'buyer@example.test',
            'password':'long-test-password-2026'})
        with store.db() as c:
            self.uid=c.execute("SELECT id FROM users WHERE email='buyer@example.test'").fetchone()['id']
    def tearDown(self):StoreTests.tearDown(self)

    def test_account_uses_delete_confirmation_not_link_panel(self):
        page=self.client.get('/account')
        self.assertEqual(page.status_code,200)
        self.assertIn('data-open-account-delete',page.data.decode())
        self.assertIn('data-close-account-delete',page.data.decode())
        self.assertNotIn('ربط حسابات الدخول',page.data.decode())
        self.assertIn('btn btn-danger',page.data.decode())
        self.assertIn('btn btn-safe',page.data.decode())

    def test_confirmation_and_csrf_required(self):
        invalid=[
            {'csrf':self.token(),'confirm_email':'buyer@example.test','confirm_word':'other'},
            {'csrf':self.token(),'confirm_email':'other@example.test','confirm_word':'حذف'},
            {'csrf':'wrong','confirm_email':'buyer@example.test','confirm_word':'حذف'},
        ]
        for data in invalid:
            response=self.client.post('/account/delete',data=data)
            self.assertIn(response.status_code,(302,400))
            with store.db() as conn:
                self.assertIsNotNone(conn.execute('SELECT id FROM users WHERE id=?',(self.uid,)).fetchone())

    def test_recent_login_required(self):
        with self.client.session_transaction() as ses:
            ses['recent_auth_at']=time.time()-3600
        resp=self.client.post('/account/delete',data={
            'csrf':self.token(),'confirm_email':'buyer@example.test','confirm_word':'حذف'})
        self.assertEqual(resp.status_code,302)
        self.assertEqual(resp.location,'/account')
        with store.db() as conn:
            self.assertIsNotNone(conn.execute('SELECT id FROM users WHERE id=?',(self.uid,)).fetchone())

    def test_delete_detaches_orders_but_preserves_financial_history(self):
        with store.db() as conn:
            conn.execute('INSERT INTO orders(reference,user_id,customer,phone,governorate,address,shipping_fee,total) VALUES(?,?,?,?,?,?,?,?)',
                         ('SH-HISTORY',self.uid,'Customer','01012345678','القاهرة','عنوان','5000','20000'))
            conn.execute('INSERT INTO favorites(user_id,product_id) VALUES(?,1)',(self.uid,))
            conn.execute("INSERT INTO product_reviews(product_id,user_id,rating,comment) VALUES(1,?,5,'جيد')",(self.uid,))
        resp=self.client.post('/account/delete',data={
            'csrf':self.token(),'confirm_email':'buyer@example.test','confirm_word':'حذف'})
        self.assertEqual(resp.status_code,302)
        self.assertEqual(resp.location,'/')
        with store.db() as conn:
            self.assertIsNone(conn.execute('SELECT id FROM users WHERE id=?',(self.uid,)).fetchone())
            order=conn.execute("SELECT user_id,total,reference FROM orders WHERE reference='SH-HISTORY'").fetchone()
            self.assertIsNone(order['user_id'])
            self.assertEqual(order['total'],20000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM favorites WHERE user_id=?',(self.uid,)).fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM product_reviews WHERE user_id=?',(self.uid,)).fetchone()[0],0)
        self.assertEqual(self.client.get('/account').status_code,302)

    def test_store_admin_cannot_delete_via_customer_route(self):
        self.client.post('/logout',data={'csrf':self.token()})
        self.client.post('/login',data={
            'csrf':self.token(),'email':'admin@example.test','password':'strong-owner-password'})
        self.assertEqual(self.client.post('/account/delete',data={
            'csrf':self.token(),'confirm_email':'admin@example.test','confirm_word':'حذف'}).status_code,403)
        self.assertNotIn('data-open-account-delete',self.client.get('/account').data.decode())

    def test_guest_cannot_delete(self):
        self.client.post('/logout',data={'csrf':self.token()})
        self.assertEqual(self.client.post('/account/delete',data={
            'csrf':self.token(),'confirm_email':'buyer@example.test','confirm_word':'حذف'}).status_code,302)

if __name__=='__main__':unittest.main()
