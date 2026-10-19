"""Margin-safe shipping through checkout and the actual supplier payload builder."""
import os,unittest
from unittest.mock import patch
import app as store
from profit_pricing import minimum_sale_price,shipping_offer,preserve_market_price
from safka_bridge import import_products,CatalogValidationError
from safka_dry_run import prepare_dry_run
from safka_live_orders import OrderSubmissionOutcome,build_payload
from tests import test_safka_checkout as checkout_fixture

# Reuse fixture setup only: do not inherit/re-run its existing test methods.
class MarginShippingTests(unittest.TestCase):
 token=checkout_fixture.AutomaticCheckoutTests.token
 def setUp(self):
  checkout_fixture.AutomaticCheckoutTests.setUp(self)
  with store.db() as c:c.execute("INSERT INTO store_settings(key,value) VALUES('margin_shipping_discount_enabled','1')")
  self.client.get('/checkout')
  self.requote()
 def tearDown(self):checkout_fixture.AutomaticCheckoutTests.tearDown(self)
 def requote(self):
  self.quote=self.client.get('/api/shipping-quote',query_string={k:self.delivery[k] for k in ('governorate','city','area')}).json
  self.delivery['quote_token']=self.quote['quote_token']
 def test_discount_retains_30_egp_and_supplier_payload_reconciles(self):
  self.assertEqual(self.quote['products_total_piastres'],15000)
  self.assertEqual(self.quote['shipping_discount_piastres'],3000)
  self.assertEqual(self.quote['shipping_piastres'],6500)
  sent=[]
  def sender(canonical,contract,key):sent.append(build_payload(canonical,contract));return OrderSubmissionOutcome('accepted','offline-id')
  with patch('safka_auto_orders.submit_payload',side_effect=sender):
   response=self.client.post('/checkout',data=self.delivery)
  self.assertIn('/order/success/',response.location)
  with store.db() as c:
   o=c.execute('SELECT * FROM orders').fetchone();out=c.execute('SELECT * FROM safka_order_outbox').fetchone()
   self.assertEqual((o['shipping_standard_fee'],o['shipping_discount'],o['shipping_fee']),(9500,3000,6500))
   self.assertEqual(out['submitted_commission_piastres'],3000)
   self.assertEqual(prepare_dry_run(c,o['id'])['estimated_margin_before_costs_piastres'],3000)
  self.assertEqual(len(sent),1)
 def test_changed_cost_or_price_invalidates_previously_quoted_discount(self):
  with store.db() as c:c.execute('UPDATE products SET price=16000 WHERE id=1')
  with patch('safka_auto_orders.submit_payload') as send:
   self.client.post('/checkout',data=self.delivery);send.assert_not_called()
  with store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],0)
 def test_missing_cost_or_thin_margin_never_gets_a_discount(self):
  with store.db() as c:
   c.execute('UPDATE products SET supplier_cost=NULL WHERE id=1')
   product=c.execute('SELECT * FROM products WHERE id=1').fetchone()
   self.assertEqual(shipping_offer(c,[(product,1)],(9500,8500))['discount'],0)
   c.execute('UPDATE products SET supplier_cost=14900 WHERE id=1')
   product=c.execute('SELECT * FROM products WHERE id=1').fetchone()
   self.assertEqual(shipping_offer(c,[(product,1)],(9500,8500))['discount'],0)
 def test_tampering_saved_discount_prevents_supplier_dispatch(self):
  with patch('safka_auto_orders.submit_payload',return_value=OrderSubmissionOutcome('accepted','offline')):
   self.client.post('/checkout',data=self.delivery)
  with store.db() as c:
   c.execute('UPDATE orders SET shipping_discount=shipping_discount+1')
   with self.assertRaises(CatalogValidationError):prepare_dry_run(c,1)
 def test_large_profit_can_fund_full_delivery_but_never_negative_shipping(self):
  with store.db() as c:
   c.execute('UPDATE products SET price=100000,supplier_cost=50000 WHERE id=1')
   product=c.execute('SELECT * FROM products WHERE id=1').fetchone()
   offer=shipping_offer(c,[(product,1)],(9500,8500))
   self.assertEqual((offer['fee'],offer['discount']),(0,9500))
 def test_repricing_survives_sync_and_cost_increases_preserve_floor(self):
  with store.db() as c:
   c.execute("INSERT INTO market_pricing(product_id,target_price,verified_cost,status) VALUES(1,13500,10000,'verified')")
   preserve_market_price(c,1,10000)
   self.assertEqual(c.execute('SELECT price FROM products WHERE id=1').fetchone()[0],13500)
   preserve_market_price(c,1,14000)
   self.assertGreaterEqual(c.execute('SELECT price FROM products WHERE id=1').fetchone()[0],minimum_sale_price(14000))
   self.assertEqual(c.execute('SELECT status FROM market_pricing').fetchone()[0],'cost_changed')
 def test_minimum_sale_price_uses_gross_margin_not_cost_markup(self):
  self.assertEqual(minimum_sale_price(10000),13000)
  self.assertEqual(minimum_sale_price(100000),118000)
