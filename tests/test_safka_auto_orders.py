"""Offline tests for official Safka order preflight and idempotent dispatch.

No real supplier requests, credentials, or buyer data are used.
"""
import os
import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock,patch
from safka_auto_orders import (
    active_product_property,merchant_commission,live_enabled,submit_saved_order_once
)
from safka_live_orders import OrderContractError,OrderSubmissionOutcome

PROD='675a00000000000000000001'
PROPERTY='675a00000000000000000002'
PROVIDER_PREVIEW={
 'order_reference':'TEST-ORDER-123',
 'customer_name':'Sample Buyer','customer_phone':'01000000000',
 'governorate':'الجيزة','address':'6 أكتوبر | منطقة | شارع تجريبي',
 'shipping_pricing_document_id':'675a00000000000000000003',
 'customer_products_piastres':20000,
 'estimated_supplier_products_piastres':15000,
 'customer_shipping_piastres':9500,
 'estimated_supplier_shipping_piastres':8500,
 'total_to_collect_piastres':29500,
 'items':[{
    'supplier_product_id':PROD,
    'supplier_product_code':'sample','quantity':1,
    'customer_unit_price_piastres':20000,
    'supplier_unit_price_piastres':15000,
    'choices':{}
 }]
}
FLAGS={
 'SHOPY_PUBLIC_CHECKOUT_ENABLED':'1',
 'SHOPY_SAFKA_ORDER_PUSH_ACTIVE':'1',
 'SHOPY_SAFKA_ORDER_SCHEMA_VERIFIED':'1',
 'SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED':'1',
 'SHOPY_SAFKA_API_KEY':'test-key-for-offline-testing-only'
}
class SafkaDispatchTests(unittest.TestCase):
 def test_no_unverified_property_guess(self):
  with self.assertRaises(OrderContractError):
   active_product_property({'properties':[{'_id':PROPERTY},{'_id':PROD}]},{})
  with self.assertRaises(OrderContractError):
   active_product_property({'properties':[{'_id':PROPERTY}]},{'المقاس':'L'})
  self.assertEqual(active_product_property({'properties':[{'_id':PROPERTY}]},{}),PROPERTY)
 def test_no_commission_without_policy(self):
  with patch.dict(os.environ,{'SHOPY_SAFKA_COMMISSION_POLICY_VERIFIED':'0'}):
   with self.assertRaises(OrderContractError):
    merchant_commission(PROVIDER_PREVIEW)
  with patch.dict(os.environ,FLAGS):
   self.assertEqual(merchant_commission(PROVIDER_PREVIEW),50.0)
 def test_submission_flags_all_required(self):
  with patch.dict(os.environ,FLAGS):
   self.assertTrue(live_enabled())
   with patch.dict(os.environ,{'SHOPY_PUBLIC_CHECKOUT_ENABLED':'0'}):
    self.assertFalse(live_enabled())
 def test_one_claim_and_one_supplier_request_with_verified_product(self):
  class MemoryConnection:
   def execute(self,*args,**kwargs):pass
  @contextmanager
  def fake_db():
   yield MemoryConnection()
  contract={
    'schema_verified':True,'path':'/api/v1/public/orders',
    'fields':{
      'client_name':'customer_name','client_phone1':'customer_phone',
      'client_address':'address',
      'shipping_governorate':'shipping_pricing_document_id',
      'commission':'requested_commission_egp','total':'total_to_collect_egp'
    },
    'items_field':'items',
    'item_fields':{'product':'supplier_product_id','property':'supplier_property_id','qty':'quantity'},
    'response_order_id_path':'data._id',
    'stringify_quantity':True
  }
  calls=[]
  def fake_send(canonical,provider_contract,key):
   calls.append(canonical)
   self.assertEqual(canonical['requested_commission_egp'],50)
   self.assertEqual(canonical['items'][0]['supplier_property_id'],PROPERTY)
   return OrderSubmissionOutcome('accepted','provider-test-id')
  with patch.dict(os.environ,FLAGS),\
       patch('safka_auto_orders.prepare_dry_run',return_value=PROVIDER_PREVIEW),\
       patch('safka_auto_orders.load_contract',return_value=contract),\
       patch('safka_auto_orders.claim_one_test_order') as claim,\
       patch('safka_auto_orders.record_outcome') as record:
   out=submit_saved_order_once(fake_db,42,
      detail_fetch=lambda key,pid:({'supplier_id':PROD,'supplier_cost':15000},
                  {'is_active':True,'properties':[{'_id':PROPERTY}]}),
      sender=fake_send)
   self.assertEqual(out.state,'accepted')
   self.assertEqual(len(calls),1)
   claim.assert_called_once()
   record.assert_called_once()
 def test_rejects_supplier_price_change_before_claim(self):
  @contextmanager
  def fake_db():yield MagicMock()
  with patch.dict(os.environ,FLAGS),\
       patch('safka_auto_orders.prepare_dry_run',return_value=PROVIDER_PREVIEW),\
       patch('safka_auto_orders.load_contract',return_value={}),\
       patch('safka_auto_orders.claim_one_test_order') as claim:
   with self.assertRaises(OrderContractError):
    submit_saved_order_once(fake_db,42,detail_fetch=lambda key,pid:
          ({'supplier_id':PROD,'supplier_cost':17500},
           {'is_active':True,'properties':[{'_id':PROPERTY}]}))
   claim.assert_not_called()

if __name__=='__main__':
 unittest.main()
