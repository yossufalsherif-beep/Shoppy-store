"""Customer-facing favorites, ratings and delivery checkout tests (offline)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as store

class CustomerExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db_patch=patch.object(store,'DB',str(Path(self.temp.name)/'customer.sqlite3'))
        self.db_patch.start()
        store.init()
        store.app.config.update(TESTING=True,SECRET_KEY='test-review-key')
        self.client=store.app.test_client()
        with store.db() as conn:
            conn.execute("""INSERT INTO products(title,slug,description,price,image,active,options_json,category)
                         VALUES(?,?,?,?,?,?,?,?)""",
                         ('منتج عادي','simple-product','وصف',10000,'',1,'{}','منتجات متنوعة'))
            conn.execute("INSERT INTO shipping(governorate,fee) VALUES('الجيزة',7000)")
    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()
    def token(self):
        with self.client.session_transaction() as session:session['csrf']='customer-test-csrf'
        return 'customer-test-csrf'
    def register(self,email='customer@test.example'):
        return self.client.post('/register',data={'csrf':self.token(),'email':email,'password':'strong-customer-password'})
    def test_unauthenticated_wishlist_and_reviews_require_login(self):
        self.assertEqual(self.client.get('/favorites').status_code,302)
        self.assertEqual(self.client.post('/favorites/toggle/1',data={'csrf':self.token()}).status_code,302)
        self.assertEqual(self.client.post('/products/1/review',data={
            'csrf':self.token(),'rating':'5','slug':'simple-product'}).status_code,302)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM favorites').fetchone()[0],0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM product_reviews').fetchone()[0],0)
    def test_favorites_saved_for_specific_customer_only(self):
        self.register()
        self.client.post('/favorites/toggle/1',data={'csrf':self.token(),'return_to':'/favorites'})
        self.assertIn('منتج عادي'.encode(),self.client.get('/favorites').data)
        self.client.post('/logout',data={'csrf':self.token()})
        self.register('other@test.example')
        self.assertNotIn('منتج عادي'.encode(),self.client.get('/favorites').data)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM favorites').fetchone()[0],1)
    def test_one_low_rating_without_reason_is_rejected_and_valid_one_is_stored(self):
        self.register()
        for rating in ('1','2','3'):
            self.client.post('/products/1/review',data={
                'csrf':self.token(),'slug':'simple-product','rating':rating,'comment':''})
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM product_reviews').fetchone()[0],0)
        self.client.post('/products/1/review',data={
            'csrf':self.token(),'slug':'simple-product','rating':'3',
            'comment':'الخامة مش زي اللي توقعتها'})
        with store.db() as conn:
            row=conn.execute('SELECT rating,comment FROM product_reviews').fetchone()
            self.assertEqual(row['rating'],3)
            self.assertIn('الخامة',row['comment'])
        view=self.client.get('/products/simple-product')
        self.assertEqual(view.status_code,200)
        self.assertIn('الخامة مش زي اللي توقعتها'.encode(),view.data)
    def test_high_rating_can_have_no_comment_and_edit_upserts(self):
        self.register()
        self.client.post('/products/1/review',data={
            'csrf':self.token(),'slug':'simple-product','rating':'5','comment':''})
        self.client.post('/products/1/review',data={
            'csrf':self.token(),'slug':'simple-product','rating':'4','comment':''})
        with store.db() as conn:
            reviews=conn.execute('SELECT rating,comment FROM product_reviews').fetchall()
            self.assertEqual(len(reviews),1)
            self.assertEqual(reviews[0]['rating'],4)
    def test_checkout_form_has_delivery_details_only_and_no_manual_supplier_inputs(self):
        self.register()
        self.client.post('/cart/add/1',data={'csrf':self.token()})
        response=self.client.get('/checkout')
        self.assertEqual(response.status_code,200)
        html=response.data.decode()
        for field in ('name="name"','name="phone"','name="governorate"','name="city"',
                      'name="area"','name="address"'):
            self.assertIn(field,html)
        for internal in ('name="property_id"','name="commission"','name="supplier_product_id"'):
            self.assertNotIn(internal,html)
        self.assertIn('سعر المنتجات',html)
    def test_review_csrf_and_escape(self):
        self.register()
        self.assertEqual(self.client.post('/products/1/review',data={
            'csrf':'incorrect','slug':'simple-product','rating':'5'}).status_code,400)
        self.client.post('/products/1/review',data={
            'csrf':self.token(),'slug':'simple-product','rating':'5',
            'comment':'<script>bad()</script>'})
        page=self.client.get('/products/simple-product').data.decode()
        self.assertNotIn('<script>bad()</script>',page)
        self.assertIn('&lt;script&gt;',page)

if __name__=='__main__':unittest.main()
