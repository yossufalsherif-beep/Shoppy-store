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

    def test_one_item_charges_flat_fifty_egp_without_shipping_discount(self):
        self.assertEqual(self.quote['products_total_piastres'], 24500)
        self.assertEqual(self.quote['bundle_discount_piastres'], 0)
        self.assertEqual(self.quote['shipping_piastres'], 5000)
        self.assertEqual(self.quote['standard_shipping_piastres'], 5000)
        self.assertEqual(self.quote['shipping_discount_piastres'], 0)
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
        self.assertEqual(offer['bundle_offer_version'], 3)
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
        snapshot['bundle_offer_version'] = 3
        snapshot['retained_profit_floor'] = 100
        validate_delivery_snapshot(snapshot, 49000, 30000, quantity=2)


    def test_flat_fifty_shipping_for_near_and_far_supplier_tariffs(self):
        p = {'price': 15000, 'supplier_cost': 15000}
        with store.db() as conn:
            for rate in ((9500, 8500), (11000, 10000), (12000, 11000)):
                with self.subTest(rate=rate):
                    offer = checkout_offer(conn, [(p, 1, {}, 'one')], rate)
                    self.assertEqual(offer['fee'], 5000)
                    self.assertEqual(offer['bundle_discount'], 0)
                    self.assertEqual(offer['bundle_offer_version'], 3)
                    self.assertGreaterEqual(
                        offer['merchandise_total'] + offer['fee'] - p['supplier_cost'] - rate[1],
                        100)

    def test_adjust_cart_quantity_one_at_a_time_and_recalculate_offer(self):
        self.client.post('/cart/add/1', data={'csrf': self.token()})
        with self.client.session_transaction() as sess:
            key = next(iter(sess['cart']))
            self.assertEqual(sess['cart'][key], 2)
        self.requote()
        self.assertEqual(self.quote['shipping_piastres'], 0)
        self.assertEqual(self.quote['bundle_discount_piastres'], 9500)
        response = self.client.post('/cart/quantity', data={
            'csrf': self.token(), 'key': key, 'action': 'decrease'})
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['cart'][key], 1)
        self.requote()
        self.assertEqual(self.quote['shipping_piastres'], 5000)
        self.assertEqual(self.quote['bundle_discount_piastres'], 0)
        self.client.post('/cart/quantity', data={
            'csrf': self.token(), 'key': key, 'action': 'increase'})
        self.requote()
        self.assertEqual(self.quote['shipping_piastres'], 0)
        self.assertEqual(self.quote['bundle_discount_piastres'], 9500)
        self.client.post('/cart/quantity', data={
            'csrf': self.token(), 'key': key, 'action': 'decrease'})
        self.client.post('/cart/quantity', data={
            'csrf': self.token(), 'key': key, 'action': 'decrease'})
        with self.client.session_transaction() as sess:
            self.assertNotIn(key, sess['cart'])

    def test_quantity_endpoint_rejects_unknown_key_or_invalid_action(self):
        with self.client.session_transaction() as sess:
            key = next(iter(sess['cart']))
        for data in (
            {'csrf': self.token(), 'key': 'another-product', 'action': 'increase'},
            {'csrf': self.token(), 'key': key, 'action': 'delete-all'},
            {'csrf': 'wrong-token', 'key': key, 'action': 'decrease'},
        ):
            with self.subTest(data=data):
                r = self.client.post('/cart/quantity', data=data)
                self.assertEqual(r.status_code, 400)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess['cart'][key], 1)

    def test_checkout_amounts_display_western_digits(self):
        checkout = self.client.get('/checkout').data.decode('utf-8')
        cart = self.client.get('/cart').data.decode('utf-8')
        self.assertIn("toLocaleString('en-US'", checkout)
        self.assertNotIn("toLocaleString('ar-EG'", checkout)
        self.assertIn('id="grand-total" dir="ltr"', checkout)
        self.assertIn('/cart/quantity', cart)
        self.assertIn('name="action" value="decrease"', cart)
        self.assertIn('name="action" value="increase"', cart)
        self.assertIn('50 جنيه', cart)


    def test_product_links_show_compact_offer_without_hiding_product_cta(self):
        with store.db() as conn:
            slug = conn.execute('SELECT slug FROM products WHERE id=1').fetchone()['slug']
        page = self.client.get('/products/' + slug)
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'mini-promo-product', page.data)
        self.assertIn('تفاصيل العرض'.encode('utf-8'), page.data)
        self.assertIn('خصم 95 جنيه'.encode('utf-8'), page.data)
        self.assertIn('action="/cart/add/1"'.encode('utf-8'), page.data)

    def test_cart_banner_reuses_home_copy_without_pressuring_to_add_items(self):
        home = self.client.get('/')
        self.assertEqual(home.status_code, 200)
        copy = [
            'قطعتين أو أكتر؟', 'الشحن علينا!',
            'كل قطعة إضافية بعد الأولى', 'نفس المنتج مرتين أو أكتر',
            'ومش لازم تشتري منتجات مختلفة.',
            'والشحن مجاني للطلبات المؤهلة من قطعتين فأكثر.',
            'عند شراء قطعة واحدة، الشحن 50 جنيه فقط لأي محافظة أو مدينة مدعومة.',
        ]
        for count in (1, 2, 3):
            with self.subTest(count=count):
                cart = self.client.get('/cart')
                self.assertEqual(cart.status_code, 200)
                self.assertIn(b'mini-promo-cart', cart.data)
                for phrase in copy:
                    self.assertIn(phrase.encode('utf-8'), home.data)
                    self.assertIn(phrase.encode('utf-8'), cart.data)
                self.assertNotIn('ضيف قطعة تانية'.encode('utf-8'), cart.data)
                self.assertNotIn('عرضك اتفعّل'.encode('utf-8'), cart.data)
                self.assertIn('name="action" value="decrease"'.encode('utf-8'), cart.data)
                if count < 3:
                    self.client.post('/cart/add/1', data={'csrf': self.token()})

    def test_product_promo_is_shorter_and_neutral(self):
        with store.db() as conn:
            slug = conn.execute('SELECT slug FROM products WHERE id=1').fetchone()['slug']
        page = self.client.get('/products/' + slug)
        self.assertEqual(page.status_code, 200)
        self.assertIn('خصم 95 جنيه على كل قطعة إضافية بعد الأولى، وشحن مجاني من قطعتين.'.encode('utf-8'), page.data)
        self.assertNotIn('اشتري قطعتين أو أكتر:'.encode('utf-8'), page.data)
        self.assertNotIn('ضيف قطعة تانية'.encode('utf-8'), page.data)

    def test_compact_promotions_off_when_campaign_flag_is_disabled(self):
        with store.db() as conn:
            slug = conn.execute('SELECT slug FROM products WHERE id=1').fetchone()['slug']
        with patch.dict(os.environ, {'SHOPY_BUNDLE_OFFER_ENABLED': '0'}):
            self.assertNotIn(b'mini-promo-cart', self.client.get('/cart').data)
            self.assertNotIn(b'mini-promo-product', self.client.get('/products/' + slug).data)

if __name__=='__main__':
    unittest.main()
