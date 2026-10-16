import json
import sqlite3
import unittest
from unittest.mock import Mock
from urllib.error import URLError

from safka_live_orders import (validate_contract, OrderContractError, canonical_order,
                               build_payload, submit_payload, claim_one_test_order,
                               record_outcome, OrderSubmissionOutcome)

# Not asserted as a genuine Safka endpoint/schema; fixture strictly for MOCK tests.
MOCK_CONTRACT = {
 'schema_verified':True,
 'path':'/api/v1/public/mock-order',
 'fields':{
   'buyer':'customer_name','telephone':'customer_phone','province':'governorate',
   'location':'address','amount':'total_to_collect_egp'
 },
 'items_field':'lines',
 'item_fields':{'supplierId':'supplier_product_id','supplierCode':'supplier_product_code','qty':'quantity'},
 'response_order_id_path':'result.orderId'
}

PREVIEW = {
 'order_reference':'SH-TEST001','customer_name':'اختبار','customer_phone':'01000000000',
 'governorate':'الجيزة','address':'اختبار منزل',
 'customer_products_piastres':14000,'customer_shipping_piastres':5000,
 'estimated_supplier_shipping_piastres':4000,'total_to_collect_piastres':19000,
 'items':[{'supplier_product_id':'A-1','supplier_product_code':'0001234',
           'quantity':1,'customer_unit_price_piastres':14000,
           'supplier_unit_price_piastres':10000,'choices':{}}],
}

class DummyResponse:
 status=201
 def __init__(self, payload):self.payload=payload
 def __enter__(self):return self
 def __exit__(self,*args):pass
 def read(self,n):return json.dumps(self.payload).encode('utf8')

class DummyOpener:
 def __init__(self, payload=None, exception=None):
  self.payload=payload;self.exception=exception;self.calls=[]
 def open(self, request, timeout):
  self.calls.append(request)
  if self.exception:raise self.exception
  return DummyResponse(self.payload)

class TestV43(unittest.TestCase):
 def test_cannot_guess_contract(self):
  with self.assertRaises(OrderContractError):validate_contract({'schema_verified': False})
 def test_refuse_external_provider_path(self):
  c={**MOCK_CONTRACT,'path':'https://evil.example/orders'}
  with self.assertRaises(OrderContractError):validate_contract(c)
 def test_no_missing_total_field(self):
  c={**MOCK_CONTRACT,'fields':{'name':'customer_name'}}
  with self.assertRaises(OrderContractError):validate_contract(c)
 def test_local_totals_and_not_false_commission(self):
  canonical=canonical_order(PREVIEW)
  self.assertEqual(canonical['customer_shipping_egp'],50)
  self.assertEqual(canonical['supplier_shipping_egp'],40)
  self.assertEqual(canonical['total_to_collect_egp'],190)
  self.assertNotIn('commission',canonical)
 def test_provider_mock_payload(self):
  payload=build_payload(canonical_order(PREVIEW),MOCK_CONTRACT)
  self.assertEqual(payload['amount'],190)
  self.assertEqual(payload['lines'][0]['supplierCode'],'0001234')
 def test_options_block_unverified_variants(self):
  p=json.loads(json.dumps(PREVIEW));p['items'][0]['choices']={'color':'black'}
  with self.assertRaises(OrderContractError):build_payload(canonical_order(p),MOCK_CONTRACT)
 def test_code_required(self):
  p=json.loads(json.dumps(PREVIEW));p['items'][0]['supplier_product_code']=None
  with self.assertRaises(OrderContractError):build_payload(canonical_order(p),MOCK_CONTRACT)
 def test_submit_mock_sends_single_post_to_fixed_host(self):
  opener=DummyOpener({'success':True,'result':{'orderId':'SF-110'}})
  result=submit_payload(canonical_order(PREVIEW),MOCK_CONTRACT,'abcdefghijklmnop',opener=opener)
  self.assertEqual(result.state,'accepted')
  self.assertEqual(result.provider_order_id,'SF-110')
  self.assertEqual(len(opener.calls),1)
  self.assertEqual(opener.calls[0].full_url,'https://api.safka-eg.com/api/v1/public/mock-order')
  self.assertEqual(opener.calls[0].get_method(),'POST')
 def test_unconfirmed_response_is_unknown_not_success(self):
  opener=DummyOpener({'success':True})
  result=submit_payload(canonical_order(PREVIEW),MOCK_CONTRACT,'abcdefghijklmnop',opener=opener)
  self.assertEqual(result.state,'unknown')
 def test_network_timeout_unknown_no_retry(self):
  opener=DummyOpener(exception=URLError('timeout'))
  self.assertEqual(submit_payload(canonical_order(PREVIEW),MOCK_CONTRACT,'abcdefghijklmnop',opener=opener).state,'unknown')
  self.assertEqual(len(opener.calls),1)
 def test_idempotent_db_claim(self):
  db=sqlite3.connect(':memory:');db.row_factory=sqlite3.Row
  db.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY,reference TEXT,status TEXT)')
  db.execute("CREATE TABLE safka_order_outbox(order_id INTEGER PRIMARY KEY,status TEXT,error TEXT,attempt_count INTEGER,updated_at TEXT,supplier_order_id TEXT)")
  db.execute("INSERT INTO orders VALUES(1,'SH-TEST001','new')")
  db.execute("INSERT INTO safka_order_outbox VALUES(1,'awaiting_provider_configuration','',0,'',NULL)")
  claim_one_test_order(db,1,'SH-TEST001')
  with self.assertRaises(OrderContractError):claim_one_test_order(db,1,'SH-TEST001')
  record_outcome(db,1,OrderSubmissionOutcome('unknown'))
  self.assertEqual(db.execute('SELECT attempt_count,status FROM safka_order_outbox').fetchone()['attempt_count'],1)
  with self.assertRaises(OrderContractError):claim_one_test_order(db,1,'SH-TEST001')
  db.close()

if __name__=='__main__':unittest.main()
