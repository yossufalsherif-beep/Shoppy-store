"""Bounded pages, combined filters, guest favorites and AJAX cart behavior."""
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash
import app as store

class PagingTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.patcher=patch.object(store,'DB',str(Path(self.temp.name)/'ui.sqlite3'));self.patcher.start()
  store.init();store.app.config.update(TESTING=True,SECRET_KEY='ui-testing-only-secret')
  self.client=store.app.test_client()
  with store.db() as c:
   c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',('owner@test.invalid',generate_password_hash('owner-testing-password'),'admin'))
   for n in range(1,66):
    c.execute('INSERT INTO products(title,slug,price,category) VALUES(?,?,?,?)',('منتج '+str(n),'product-'+str(n),n*1000,'أول' if n<=40 else 'ثان'))
   for n in range(1,46):
    c.execute('INSERT INTO orders(reference,customer,phone,governorate,address,shipping_fee,total,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
      ('SH-PAGE-'+str(n).zfill(3),'أحمد' if n%2 else 'يوسف','010'+str(n).zfill(8),'القاهرة' if n%2 else 'الجيزة','عنوان اختبار',5000,n*10000,'new' if n%2 else 'shipped','2026-10-'+str(1+(n%15)).zfill(2)+' 12:00:00'))
 def tearDown(self):self.patcher.stop();self.temp.cleanup()
 def csrf(self):
  with self.client.session_transaction() as s:s['csrf']='ui-csrf'
  return 'ui-csrf'
 def owner(self):self.client.post('/login',data={'csrf':self.csrf(),'email':'owner@test.invalid','password':'owner-testing-password'})
 def product_ids(self,response):return re.findall(r'data-product-id="(\d+)"',response.data.decode())
 def order_refs(self,response):return re.findall(r'data-order-reference="([^"]+)"',response.data.decode())
 def test_catalog_is_limited_to_30_and_category_page_two_has_10(self):
  self.assertEqual(len(self.product_ids(self.client.get('/'))),30)
  self.assertEqual(len(self.product_ids(self.client.get('/?category=أول&page=2'))),10)
  self.assertEqual(len(self.product_ids(self.client.get('/?page=3'))),5)
  self.assertEqual(len(self.product_ids(self.client.get('/?page=9999999999999'))),5)
 def test_price_search_category_sort_and_links_preserve_all_filters(self):
  response=self.client.get('/',query_string={'category':'أول','min_price':'100','max_price':'200','sort':'price_low','q':'منتج'})
  self.assertEqual(self.product_ids(response),[str(n) for n in range(10,21)])
  response=self.client.get('/',query_string={'category':'أول','min_price':'10','max_price':'400','sort':'price_low'})
  html=response.data.decode();self.assertIn('min_price=10',html);self.assertIn('max_price=400',html);self.assertIn('sort=price_low',html)
  self.assertIn('category=',html);self.assertIn('page=2',html)
 def test_invalid_price_and_injection_do_not_break_query(self):
  self.assertEqual(self.client.get('/?min_price=NaN').status_code,200)
  self.assertEqual(self.product_ids(self.client.get('/',query_string={'q':"' OR 1=1--"})),[])
 def test_admin_orders_are_oldest_first_and_20_per_page_products_30(self):
  self.owner();first=self.client.get('/admin');second=self.client.get('/admin?orders_page=2&products_page=2')
  self.assertEqual(self.order_refs(first),['SH-PAGE-'+str(n).zfill(3) for n in range(1,21)])
  self.assertEqual(self.order_refs(second),['SH-PAGE-'+str(n).zfill(3) for n in range(21,41)])
  self.assertEqual(first.data.count(b'<details class="manage-item">'),30)
  self.assertEqual(second.data.count(b'<details class="manage-item">'),30)
  self.assertEqual(self.order_refs(self.client.get('/admin?order_sort=newest'))[0],'SH-PAGE-045')
 def test_order_search_code_name_phone_and_combined_filters(self):
  self.owner()
  self.assertEqual(self.order_refs(self.client.get('/admin?order_q=SH-PAGE-032&order_field=reference')),['SH-PAGE-032'])
  self.assertEqual(self.order_refs(self.client.get('/admin?order_q=01000000032&order_field=phone')),['SH-PAGE-032'])
  response=self.client.get('/admin',query_string={'order_q':'يوسف','order_field':'name','order_status':'shipped','order_governorate':'الجيزة','order_min':'3000','order_max':'3500'})
  self.assertEqual(self.order_refs(response),['SH-PAGE-030','SH-PAGE-032','SH-PAGE-034'])
  response=self.client.get('/admin?order_from=2026-10-01&order_to=2026-10-01')
  self.assertEqual(self.order_refs(response),['SH-PAGE-015','SH-PAGE-030','SH-PAGE-045'])
 def test_admin_relationship_query_budget_does_not_scale_with_catalog(self):
  self.owner();base=store.db;queries=[]
  def traced():
   conn=base();conn.set_trace_callback(queries.append);return conn
  with patch.object(store,'db',side_effect=traced):self.client.get('/admin')
  before=len([q for q in queries if q.lstrip().upper().startswith('SELECT')])
  with base() as c:
   for n in range(66,366):c.execute('INSERT INTO products(title,slug,price) VALUES(?,?,?)',('More '+str(n),'more-'+str(n),1000))
  queries.clear()
  with patch.object(store,'db',side_effect=traced):self.client.get('/admin')
  after=len([q for q in queries if q.lstrip().upper().startswith('SELECT')])
  self.assertLessEqual(after,before+2);self.assertLess(after,45)
 def test_guest_heart_and_ajax_add_work_without_navigation_or_order_creation(self):
  headers={'Accept':'application/json'}
  favorite=self.client.post('/favorites/toggle/1',data={'csrf':self.csrf()},headers=headers)
  self.assertTrue(favorite.json['favorite']);self.assertEqual(favorite.json['view_url'],'/favorites')
  self.assertIn('منتج 1'.encode(),self.client.get('/favorites').data)
  added=self.client.post('/cart/add/1',data={'csrf':self.csrf()},headers=headers)
  self.assertTrue(added.json['ok']);self.assertEqual(added.json['cart_count'],1)
  with store.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],45)
 def test_guest_favorites_merge_into_customer_account(self):
  self.client.post('/favorites/toggle/1',data={'csrf':self.csrf()})
  self.client.post('/register',data={'csrf':self.csrf(),'email':'guest@test.invalid','password':'guest-testing-password'})
  with store.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM favorites WHERE product_id=1').fetchone()[0],1)

