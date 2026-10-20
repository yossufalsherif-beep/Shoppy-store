"""Offline tests for the explicit 95 EGP item premium and two-item promotion."""
import os
import unittest
from unittest.mock import patch
import app as store
from profit_pricing import displayed_product_price, checkout_offer, retained_profit_floor, validate_delivery_snapshot
from safka_dry_run import prepare_dry_run
from safka_bridge import CatalogValidationError
from safka_live_orders import OrderSubmissionOutcome, build_payload
from tests import test_safka_checkout as checkout_fixture


class TwoPieceOfferTests(unittest.TestCase):
    token = checkout_fixture.AutomaticCheckoutTests.token

    def setUp(self):
        checkout_fixture.AutomaticCheckoutTests.setUp(self)
        self.promotion = patch.dict(os.environ, {
            "SHOPY_BUNDLE_OFFER_ENABLED": "1",
            "SHOPY_SINGLE_ITEM_SHIPPING_REDUCTION_EGP": "45",
        })
        self.promotion.start()
        self.client.get('/checkout')
        self.requote()

    def tearDown(self):
        self.promotion.stop()
        checkout_fixture.AutomaticCheckoutTests.tearDown(self)

    def requote(self):
        self.quote = self.client.get('/api/shipping-quote', query_string={
            k: self.delivery[k] for k in ('governorate', 'city', 'area')
        }).json
        self.assertTrue(self.quote['ok'], self.quote)
        self.delivery['quote_token'] = self.quote['quote_token']

    def test_home_banner_mentions_same_product_quantities(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('نفس المنتج مرتين أو أكتر'.encode('utf-8'), page.data)
        self.assertIn('كل قطعة إضافية بعد الأولى'.encode('utf-8'), page.data)
        self.assertIn('الشحن علينا'.encode('utf-8'), page.data)

    def test_one_item_shows_region_based_reduced_delivery(self):
        self.assertEqual(self.quote['products_total_piastres'], 24500)
        self.assertEqual(self.quote['bundle_discount_piastres'], 0)
        self.assertEqual(self.quote['shipping_piastres'], 5000)
        self.assertEqual(self.quote['standard_shipping_piastres'], 9500)
        self.assertEqual(self.quote['merchandise_total_piastres'], 24500)
        self.assertEqual(displayed_product_price({'price':15000}),24500)

    def test_two_items_get_95_discount_each_additional_and_free_shipping(self):
        self.client.post('/cart/add/1', data={'csrf':self.token()})
        self.requote()
        self.assertEqual(self.quote['bundle_unit_count'], 2)
        self.assertEqual(self.quote['products_total_piastres'], 49000)
        self.assertEqual(self.quote['bundle_discount_piastres'], 9500)
        self.assertEqual(self.quote['shipping_piastres'], 0)
        self.assertEqual(self.quote['merchandise_total_piastres'], 39500)
        with patch('safka_auto_orders.submit_payload',
                   return_value=OrderSubmissionOutcome('accepted','offline-id')) as send:
            response = self.client.post('/checkout', data=self.delivery)
        self.assertIn('/order/success/',response.location)
        send.assert_called_once()
        with store.db() as conn:
            order=conn.execute('SELECT * FROM orders').fetchone()
            item=conn.execute('SELECT * FROM order_items').fetchone()
            preview=prepare_dry_run(conn,order['id'])
            outbox=conn.execute('SELECT * FROM safka_order_outbox').fetchone()
            self.assertEqual((order['total'],order['shipping_fee'],order['bundle_discount']),(39500,0,9500))
            self.assertEqual((item['unit_price'],item['quantity']),(24500,2))
            self.assertEqual(preview['customer_products_piastres'],39500)
            self.assertEqual(outbox['submitted_commission_piastres'],11000)

    def test_quote_bound_to_quantity_and_discount(self):
        self.client.post('/cart/add/1', data={'csrf':self.token()})
        with patch('safka_auto_orders.submit_payload') as send:
            self.client.post('/checkout', data=self.delivery)
            send.assert_not_called()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM orders').fetchone()[0],0)

    def test_snapshot_tampering_blocks_supplier_order(self):
        self.client.post('/cart/add/1', data={'csrf':self.token()})
        self.requote()
        with patch('safka_auto_orders.submit_payload',
                   return_value=OrderSubmissionOutcome('accepted','offline-id')):
            self.client.post('/checkout', data=self.delivery)
        with store.db() as conn:
            conn.execute('UPDATE orders SET bundle_discount=bundle_discount+100')
            with self.assertRaises(CatalogValidationError):
                prepare_dry_run(conn,1)

    def test_missing_supplier_cost_does_not_publish_fake_free_shipping(self):
        with store.db() as conn:
            conn.execute('UPDATE products SET supplier_cost=NULL WHERE id=1')
        result=self.client.get('/api/shipping-quote',query_string={
            k:self.delivery[k] for k in ('governorate','city','area')
        })
        self.assertEqual(result.status_code,409)
        self.assertFalse(result.json['ok'])

    def test_three_pieces_still_add_only_one_net_premium(self):
        self.client.post('/cart/add/1', data={'csrf':self.token()})
        self.client.post('/cart/add/1', data={'csrf':self.token()})
        self.requote()
        self.assertEqual(self.quote['products_total_piastres'],73500)
        self.assertEqual(self.quote['bundle_discount_piastres'],19000)
        self.assertEqual(self.quote['merchandise_total_piastres'],54500)
        self.assertEqual(self.quote['shipping_piastres'],0)


    def test_distant_destination_can_keep_small_positive_margin(self):
        # 2 x EGP 150, plus one net EGP 95 premium, supplier delivery EGP 85.
        # EGP 10 merchant contribution is BELOW the legacy 15% floor but profitable.
        product = {'price': 15000, 'supplier_cost': 15000}
        with store.db() as conn:
            offer = checkout_offer(conn, [(product, 2, {}, 'same-sku')], (9500, 8500))
        self.assertEqual(offer['displayed_subtotal'], 49000)
        self.assertEqual(offer['bundle_discount'], 9500)
        self.assertEqual(offer['merchandise_total'], 39500)
        self.assertEqual(offer['fee'], 0)
        self.assertEqual(offer['bundle_offer_version'], 2)
        self.assertEqual(offer['floor'], 100)
        profit = offer['merchandise_total'] - 2 * product['supplier_cost'] - 8500
        self.assertEqual(profit, 1000)
        self.assertLess(profit, retained_profit_floor(offer['merchandise_total']))

    def test_distant_destination_still_rejects_break_even_or_direct_loss(self):
        for cost in (15500, 16000):
            with self.subTest(cost=cost):
                product = {'price': 15000, 'supplier_cost': cost}
                with store.db() as conn:
                    with self.assertRaises(ValueError):
                        checkout_offer(conn, [(product, 2, {}, 'same-sku')], (9500, 8500))

    def test_old_version_one_orders_still_retain_historical_margin_rule(self):
        snapshot = {
            'bundle_offer_version': 1,
            'shipping_standard_fee': 9500,
            'supplier_shipping_piastres': 8500,
            'customer_shipping_piastres': 0,
            'shipping_discount': 9500,
            'bundle_discount': 9500,
            'bundle_unit_count': 2,
            'retained_profit_floor': retained_profit_floor(39500),
        }
        with self.assertRaises(ValueError):
            validate_delivery_snapshot(snapshot, 49000, 30000, quantity=2)
        snapshot['bundle_offer_version'] = 2
        snapshot['retained_profit_floor'] = 100
        validate_delivery_snapshot(snapshot, 49000, 30000, quantity=2)

if __name__=='__main__':
    unittest.main()
