"""Shipping pagination, provider stock and complete customer submission. No live writes."""
import os
import json
import unittest
from unittest.mock import patch
from safka_checkout import shipping_snapshot, ensure_shipping
from safka_bridge import CatalogValidationError, normalize_product
from safka_auto_orders import active_product_property, verify_checkout_products
from safka_live_orders import OrderContractError, OrderSubmissionOutcome, build_payload
import tests.test_safka_buyer_flow as buyer_fixture
import app as store

PRODUCT='675a00000000000000000001'
PROPERTY='675a00000000000000000002'
FLAGS={
 'SHOPY_PUBLIC_CHECKOUT_ENABLED':'1','SHOPY_SAFKA_ORDER_PUSH_ACTIVE':'1',
 'SHOPY_SAFKA_ORDER_SCHEMA_VERIFIED':'1','SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED':'1',
 'SHOPY_SAFKA_API_KEY':'offline-test-key-not-real','SHOPY_SAFKA_READ_ENABLED':'0',
}
RAW={'_id':PRODUCT,'name':'منتج صفقة','sale_price':100,'is_active':True,
     'properties':[{'_id':PROPERTY,'key':'الاختيار الأساسي','value':8,'is_available':True,'sale_price':100}]}


class ShippingAndPropertyTests(unittest.TestCase):
 def sample(self,ident,name,city):
  return {'_id':ident,'governorateNameAr':name,'price':50,'is_active':True,
          'cities':[{'id':str(city),'city_name_ar':'مدينة '+str(city)}]}
 def test_paginated_snapshot_preserves_city_and_supplier_ids(self):
  pages=[[self.sample('a','القاهرة',1)],[self.sample('b','الجيزة',336)]]
  calls=[]
  def read(*args,**kw):
   calls.append(kw['page']);return {'itemsCount':2,'pages':2},pages[kw['page']-1]
  rows=shipping_snapshot('offline',read=read)
  self.assertEqual(calls,[1,2])
  self.assertEqual(rows[1]['city_id'],'336')
  self.assertEqual(rows[1]['_id'],'b')
 def test_all_in_one_response_does_not_fetch_reported_extra_pages(self):
  def read(*args,**kw):
   self.assertEqual(kw['page'],1)
   return {'itemsCount':2,'pages':3},[self.sample('a','القاهرة',1),self.sample('b','الجيزة',336)]
  self.assertEqual(len(shipping_snapshot('offline',read=read)),2)
 def test_total_count_includes_inactive_governorate_but_only_active_rates_import(self):
  active=self.sample('a','القاهرة',1)
  inactive=self.sample('b','الجيزة',336);inactive['is_active']=False
  rows=shipping_snapshot('offline',read=lambda *a,**k:(
      {'itemsCount':2,'totalActiveGovernorates':1,'pages':3},[active,inactive]))
  self.assertEqual(len(rows),1)
  self.assertEqual(rows[0]['governorateNameAr'],'القاهرة')
 def test_same_destination_aliases_keep_one_official_id_and_price(self):
  row=self.sample('a','القاهرة',1)
  row['cities'].append({'id':'2','city_name_ar':'مدينة 1'})
  rates=shipping_snapshot('offline',read=lambda *a,**k:({'itemsCount':1},[row]))
  self.assertEqual(len(rates),1)
  self.assertEqual(rates[0]['city_id'],'1')
 def test_conflicting_price_documents_for_same_destination_remain_blocked(self):
  first=self.sample('a','القاهرة',1);second=self.sample('b','القاهرة',1)
  second['price']=80
  with self.assertRaises(CatalogValidationError):
   shipping_snapshot('offline',read=lambda *a,**k:({'itemsCount':2},[first,second]))
 def test_incomplete_and_unknown_city_snapshots_are_rejected(self):
  with self.assertRaises(CatalogValidationError):
   shipping_snapshot('offline',read=lambda *a,**k:({'itemsCount':2},[self.sample('a','القاهرة',1)]))
  row=self.sample('a','القاهرة',1);row['cities'][0].pop('id')
  with self.assertRaises(CatalogValidationError):
   shipping_snapshot('offline',read=lambda *a,**k:({'itemsCount':1},[row]))
 def test_property_stock_and_availability_are_enforced(self):
  self.assertEqual(active_product_property(RAW,{},8),PROPERTY)
  with self.assertRaises(OrderContractError):active_product_property(RAW,{},9)
  raw=json.loads(json.dumps(RAW));raw['properties'][0]['is_available']=False
  with self.assertRaises(OrderContractError):active_product_property(raw,{},1)
 def test_equal_price_choices_use_exact_labels_and_stock(self):
  raw=json.loads(json.dumps(RAW));raw['properties'].append(
    {'_id':'675a00000000000000000004','key':'أزرق','value':3,'is_available':True,'sale_price':100})
  normalized=normalize_product(raw)
  self.assertEqual(normalized['stock'],11)
  self.assertEqual(normalized['options'],{'الاختيار':['الاختيار الأساسي','أزرق']})
  self.assertEqual(active_product_property(raw,{'الاختيار':'أزرق'},2),'675a00000000000000000004')
  with self.assertRaises(OrderContractError):active_product_property(raw,{})
 def test_total_quantity_cannot_bypass_stock_in_duplicate_rows(self):
  p={'supplier_product_id':PRODUCT,'supplier_cost':10000}
  with self.assertRaises(OrderContractError):
   verify_checkout_products([(p,5,{},''),(p,5,{},'')],detail_fetch=lambda *a:(normalize_product(RAW),RAW))


class AutomaticCheckoutTests(unittest.TestCase):
 token=buyer_fixture.SafkaBuyerFlowTests.token
 def setUp(self):
  buyer_fixture.SafkaBuyerFlowTests.setUp(self)
  self.env=patch.dict(os.environ,FLAGS);self.env.start()
  self.detail=patch('safka_auto_orders.fetch_product_detail',return_value=(normalize_product(RAW),RAW));self.detail.start()
  self.client.post('/register',data={'csrf':self.token(),'email':'flow@example.test','password':'test-long-password'})
  self.client.post('/cart/add/1',data={'csrf':self.token()})
  self.delivery={'csrf':self.token(),'name':'عميل اختبار','phone':'01012345678',
    'governorate':'الجيزة','city':'6 أكتوبر','area':'الحي السابع','address':'عنوان اختباري لا يتم إرساله فعليًا'}
  self.delivery['quote_token']=self.client.get('/api/shipping-quote',query_string={
    k:self.delivery[k] for k in ('governorate','city','area')}).json['quote_token']
 def tearDown(self):
  self.detail.stop();self.env.stop();buyer_fixture.SafkaBuyerFlowTests.tearDown(self)
 def test_customer_fields_produce_exact_payload_and_single_order(self):
  payloads=[]
  def send(canonical,contract,key):
   payloads.append(build_payload(canonical,contract))
   return OrderSubmissionOutcome('accepted','offline-provider-order')
  with self.client.session_transaction() as s:
   old_cart=dict(s['cart']);old_key=s.get('checkout_key')
  # A GET initializes the idempotency key before the browser submits.
  self.client.get('/checkout')
  with self.client.session_transaction() as s:old_key=s['checkout_key']
  with patch('safka_auto_orders.submit_payload',side_effect=send) as transport:
   response=self.client.post('/checkout',data=self.delivery)
   self.assertIn('/order/success/',response.location)
   with self.client.session_transaction() as s:s['cart']=old_cart;s['checkout_key']=old_key
   again=self.client.post('/checkout',data=self.delivery)
   self.assertEqual(again.location,response.location)
   transport.assert_called_once()
  self.assertEqual(payloads[0]['city'],'336')
  self.assertEqual(payloads[0]['total'],245)
  self.assertEqual(payloads[0]['commission'],60)
  self.assertEqual(payloads[0]['items'],[{'product':PRODUCT,'property':PROPERTY,'qty':'1'}])
  with store.db() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],1)
   outbox=c.execute('SELECT * FROM safka_order_outbox').fetchone()
   self.assertEqual(outbox['status'],'accepted')
   self.assertEqual(outbox['attempt_count'],1)
   self.assertEqual(outbox['submitted_commission_piastres'],6000)
 def test_guest_checkout_saves_order_without_account_and_uses_session_only(self):
  # A buyer can checkout without registration; a reference URL alone is not authorization.
  with self.client.session_transaction() as s:s.pop('uid',None)
  response=self.client.get('/checkout')
  self.assertEqual(response.status_code,200)
  self.assertIn('من غير الحاجة لإنشاء حساب'.encode(),response.data)
  self.delivery['quote_token']=self.client.get('/api/shipping-quote',query_string={
    k:self.delivery[k] for k in ('governorate','city','area')}).json['quote_token']
  with patch('safka_auto_orders.submit_payload',return_value=OrderSubmissionOutcome('accepted','guest-offline-id')):
   result=self.client.post('/checkout',data=self.delivery)
  self.assertIn('/order/success/',result.location)
  self.assertEqual(self.client.get(result.location).status_code,200)
  with store.db() as c:
   order=c.execute('SELECT user_id,phone FROM orders').fetchone()
   self.assertIsNone(order['user_id'])
   self.assertEqual(order['phone'],self.delivery['phone'])
  with self.client.session_transaction() as s:s.pop('my_order_refs',None)
  self.assertEqual(self.client.get(result.location).status_code,404)

 def test_changed_shipping_requires_customer_to_review_new_total(self):
  with store.db() as c:c.execute('UPDATE safka_shipping_rates SET fee=10500,supplier_fee=9500')
  with patch('safka_auto_orders.submit_payload') as send:
   response=self.client.post('/checkout',data=self.delivery)
   self.assertEqual(response.status_code,200)
   self.assertIn('تغيّرت السلة'.encode(),response.data)
   send.assert_not_called()
  with store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],0)
 def test_unknown_provider_response_is_saved_without_false_acceptance(self):
  with patch('safka_auto_orders.submit_payload',return_value=OrderSubmissionOutcome('unknown')) as send:
   response=self.client.post('/checkout',data=self.delivery)
   page=self.client.get(response.location)
   self.assertNotIn('تم إرسال الطلب إلى صفقة'.encode(),page.data)
   send.assert_called_once()
  with store.db() as c:self.assertEqual(c.execute('SELECT status FROM safka_order_outbox').fetchone()[0],'unknown')
 def test_shipping_refresh_failure_preserves_existing_rates_and_cools_down(self):
  with store.db() as c:c.execute("UPDATE safka_shipping_rates SET updated_at='2020-01-01T00:00:00+00:00'")
  def broken(*a,**k):raise OSError('offline simulated provider failure')
  with patch.dict(os.environ,{'SHOPY_SAFKA_READ_ENABLED':'1'}):
   with self.assertRaises(OSError):ensure_shipping(store.db,read=broken)
   with self.assertRaises(CatalogValidationError):ensure_shipping(store.db,read=broken)
  with store.db() as c:
   self.assertEqual(c.execute('SELECT fee FROM safka_shipping_rates').fetchone()[0],9500)
