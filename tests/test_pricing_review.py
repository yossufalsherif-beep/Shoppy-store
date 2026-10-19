import unittest
from pricing_review import build_plan
from profit_pricing import minimum_sale_price

class PricingReviewTests(unittest.TestCase):
 def product(self,price=20000,cost=14000):return {'id':1,'supplier_product_id':'supplier-1','supplier_cost':cost,'price':price}
 def test_unmatched_search_is_explicitly_cost_based_and_never_increases_price(self):
  p=build_plan([self.product()],[],[])[0]
  self.assertEqual(p['basis'],'cost');self.assertEqual(p['status'],'cost_based')
  self.assertIsNone(p['competitor_price']);self.assertEqual(p['source_url'],'')
  self.assertLess(p['target_price'],p['previous_price'])
  self.assertGreaterEqual(p['target_price'],minimum_sale_price(p['verified_cost']))
 def test_stock_or_model_unconfirmed_price_is_not_treated_as_verified(self):
  research=[{'id':1,'sources':[{'title':'منتج مشابه','url':'https://example.test/product','prices':[1]}]}]
  p=build_plan([self.product()],research,[])[0]
  self.assertEqual(p['status'],'cost_based');self.assertIsNone(p['competitor_price'])
 def test_verified_offer_keeps_floor_even_when_competitor_is_below_cost(self):
  p=build_plan([self.product()],[],[{'id':1,'competitor':100,'url':'https://example.test/product','notes':'مطابقة موثقة'}])[0]
  self.assertEqual(p['status'],'verified');self.assertEqual(p['target_price'],minimum_sale_price(14000))
 def test_already_competitive_verified_price_is_preserved(self):
  p=build_plan([self.product()],[],[{'id':1,'competitor':250,'url':'https://example.test/product','notes':'مطابقة موثقة'}])[0]
  self.assertEqual(p['target_price'],20000)
 def test_too_small_margin_is_flagged_without_raising_price(self):
  p=build_plan([self.product(price=15000,cost=14000)],[],[])[0]
  self.assertEqual(p['status'],'needs_review');self.assertIsNone(p['target_price'])
