import unittest
from unittest.mock import patch,MagicMock
from safka_webhooks import (authenticate_hook,webhook_token,valid_callback_key,
                            product_identifier,verified_availability,fetch_product_detail)
from safka_bridge import CatalogValidationError

class SafkaHookUnitTests(unittest.TestCase):
 def test_unique_kind_tokens(self):
  a=webhook_token('stable-secret-for-tests','product')
  self.assertTrue(authenticate_hook('stable-secret-for-tests','product',a))
  self.assertFalse(authenticate_hook('stable-secret-for-tests','order',a))
  self.assertFalse(authenticate_hook('different','product',a))
 def test_callback_never_accepts_different_api_key(self):
  self.assertTrue(valid_callback_key({'api_safka_key':'test-actual-api-key'},'test-actual-api-key'))
  self.assertFalse(valid_callback_key({'api_safka_key':'test-attacker-key'},'test-actual-api-key'))
 def test_product_event_does_not_trust_quantity(self):
  self.assertIsNone(product_identifier({'stock':200,'is_active':True}))
  self.assertEqual(product_identifier({'productId':'safka123','stock':500}),'safka123')
 def test_unknown_quantity_not_orderable(self):
  self.assertIsNone(verified_availability({'stock':None},{'is_active':True}))
  self.assertFalse(verified_availability({'stock':100},{'is_active':False}))
  self.assertTrue(verified_availability({'stock':2},{'is_active':True}))
 def test_reject_unverified_detail_identity(self):
  class Reply:
   def __enter__(self):return self
   def __exit__(self,*args):return False
   def read(self,n):return b'{"success":true,"data":{"_id":"not-the-requested-id","name":"Test","sale_price":100}}'
  opener=MagicMock()
  opener.open.return_value=Reply()
  with patch('safka_webhooks._SAFE_OPENER',opener):
   with self.assertRaises(CatalogValidationError):fetch_product_detail('test-api-key','requested-id')
 def test_official_product_wrapper_is_supported(self):
  class Reply:
   def __enter__(self):return self
   def __exit__(self,*args):return False
   def read(self,n):return b'{"product":{"_id":"requested-id","name":"Test","sale_price":100,"properties":[{"_id":"variant","value":8,"is_available":true}]}}'
  opener=MagicMock();opener.open.return_value=Reply()
  with patch('safka_webhooks._SAFE_OPENER',opener):
   normalized,raw=fetch_product_detail('offline-test-key','requested-id')
  self.assertEqual(normalized['supplier_id'],'requested-id')
  self.assertEqual(normalized['stock'],8)
  self.assertEqual(raw['properties'][0]['_id'],'variant')
if __name__=='__main__':unittest.main()
