"""Fixed code-level product offer (toothbrush 999 -> 499, free shipping, outside
the EGP 95 bundle), quick add-to-cart on cards, and admin navigation."""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app as store
from werkzeug.security import generate_password_hash
from profit_pricing import (checkout_offer, product_sale_badge, validate_delivery_snapshot,
                            displayed_product_price, MIN_BUNDLE_CONTRIBUTION_PIASTRES)
from safka_bridge import import_products
from safka_dry_run import prepare_dry_run
from special_offers import SPECIAL_OFFERS

OFFER_SLUG = 'safka-79797df9bf355ce5d686'
BRUSH_ID = '673a2c721548cec72da05e99'
OTHER_ID = '673a2c721548cec72da05e98'


def safka_record(ident, name, price):
    return {'_id': ident, 'name': name, 'barcode': ident[-4:], 'sale_price': price,
            'images': ['https://example.com/%s.jpg' % ident[-2:]],
            'properties': [{'key': 'قطعة', 'value': 20}], 'is_active': True}


class ProductOfferTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'SHOPY_BUNDLE_OFFER_ENABLED': '0'})
        self.env.start()
        self.temp = tempfile.TemporaryDirectory()
        self.database_patch = patch.object(store, 'DB', str(Path(self.temp.name) / 'offer.sqlite3'))
        self.database_patch.start()
        store.init()
        store.app.config.update(TESTING=True, SECRET_KEY='for-tests-only')
        self.client = store.app.test_client()
        with store.db() as conn:
            conn.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',
                         ('owner@example.test', generate_password_hash('strong-owner-password'), 'admin'))
            conn.execute('INSERT INTO shipping(governorate,fee,supplier_cost) VALUES(?,?,?)', ('القاهرة', 6000, 5000))
            # The stored DB price (300) is deliberately NOT the offer price.
            conn.execute('INSERT INTO products(title,slug,description,price,supplier_cost) VALUES(?,?,?,?,?)',
                         ('فرشاة أسنان', OFFER_SLUG, 'وصف', 30000, 25000))
            conn.execute('INSERT INTO products(title,slug,description,price,supplier_cost) VALUES(?,?,?,?,?)',
                         ('كوب', 'mug', 'وصف', 10000, 6000))
            conn.execute("INSERT INTO products(title,slug,description,price,options_json) VALUES(?,?,?,?,?)",
                         ('تيشيرت', 'tee', 'وصف', 20000, '{"المقاس":["M","L"]}'))
            ids = {r['slug']: r['id'] for r in conn.execute('SELECT id,slug FROM products')}
        self.brush, self.mug, self.tee = ids[OFFER_SLUG], ids['mug'], ids['tee']

    def tearDown(self):
        self.database_patch.stop()
        self.temp.cleanup()
        self.env.stop()

    def token(self):
        with self.client.session_transaction() as sess:
            sess['csrf'] = 'csrf-test-token'
        return 'csrf-test-token'

    def add(self, pid, times=1):
        for _ in range(times):
            self.client.post('/cart/add/%d' % pid, data={'csrf': self.token()})

    def checkout(self):
        return self.client.post('/checkout', data={
            'csrf': self.token(), 'name': 'عميل', 'phone': '01012345678', 'governorate': 'القاهرة',
            'city': 'القاهرة', 'area': 'مدينة نصر', 'address': 'شارع عشرة عمارة 4'})

    def last_order(self):
        with store.db() as conn:
            order = conn.execute('SELECT * FROM orders ORDER BY id DESC').fetchone()
            items = conn.execute('SELECT * FROM order_items WHERE order_id=?', (order['id'],)).fetchall()
        return order, items

    # --- configuration + display -----------------------------------------
    def test_offer_is_the_toothbrush_999_to_499_with_free_shipping(self):
        self.assertEqual(SPECIAL_OFFERS[OFFER_SLUG]['price'], 49900)
        self.assertEqual(SPECIAL_OFFERS[OFFER_SLUG]['compare_at'], 99900)
        self.assertTrue(SPECIAL_OFFERS[OFFER_SLUG]['free_shipping'])
        product = {'slug': OFFER_SLUG, 'price': 30000}
        self.assertEqual(product_sale_badge(product), {'compare_at': 99900, 'percent': 50})
        self.assertIsNone(product_sale_badge({'slug': 'mug', 'price': 10000}))

    def test_badge_price_and_note_on_card_and_product_page(self):
        home = self.client.get('/').get_data(as_text=True)
        self.assertIn('خصم 50%', home)
        self.assertIn('<s class="compare-price"', home)
        self.assertIn('999.00', home)
        self.assertIn('499.00', home)
        self.assertIn('عرض خاص: خصم 50% + شحن مجاني', home)
        self.assertNotIn('خصم الـ95', home)  # the 95 offer is off: don't mention it
        page = self.client.get('/products/' + OFFER_SLUG).get_data(as_text=True)
        self.assertIn('sale-badge-large', page)
        self.assertIn('499.00', page)
        self.assertIn('شحن مجاني', page)

    def test_with_bundle_on_offer_price_has_no_premium_and_note_says_outside_95(self):
        with patch.dict(os.environ, {'SHOPY_BUNDLE_OFFER_ENABLED': '1'}):
            self.assertEqual(displayed_product_price({'slug': OFFER_SLUG, 'price': 30000}), 49900)
            self.assertEqual(displayed_product_price({'slug': 'mug', 'price': 10000}), 19500)
            home = self.client.get('/').get_data(as_text=True)
            self.assertIn('مش داخل في خصم الـ95 جنيه', home)
            self.assertIn('499.00', home)
            page = self.client.get('/products/' + OFFER_SLUG).get_data(as_text=True)
            self.assertIn('مش داخل في خصم الـ95 جنيه', page)
            self.assertNotIn('mini-promo-product', page)  # bundle promo box hidden for the offer product
            self.assertIn('mini-promo-product', self.client.get('/products/mug').get_data(as_text=True))

    # --- quick add --------------------------------------------------------
    def test_quick_add_overlay_on_cards(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('class="cover-cart-form" method="post" action="/cart/add/%d" data-async="cart"' % self.mug, html)
        self.assertIn('aria-label="أضف للسلة: كوب"', html)
        # Products with options link to the product page instead of adding blindly.
        self.assertIn('<a class="cover-cart-button" href="/products/tee" aria-label="اختار المواصفات وأضف للسلة: تيشيرت"', html)
        self.assertNotIn('class="cover-cart-form" method="post" action="/cart/add/%d"' % self.tee, html)
        self.assertIn('aria-label="أضف للسلة: كوب"', self.client.get('/?q=كوب').get_data(as_text=True))

    def test_quick_add_json_updates_count_and_html_returns_to_page(self):
        response = self.client.post('/cart/add/%d' % self.mug, data={'csrf': self.token(), 'return_to': '/?q=x'},
                                    headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cart_count'], 1)
        response = self.client.post('/cart/add/%d' % self.mug, data={'csrf': self.token(), 'return_to': '/?q=x'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/?q=x'))
        self.assertEqual(self.client.post('/cart/add/%d' % self.mug, data={'csrf': 'bad'}).status_code, 400)

    # --- checkout ---------------------------------------------------------
    def test_order_with_offer_product_charges_499_and_ships_free(self):
        self.add(self.brush)
        self.add(self.mug)
        self.assertIn('مجاني — طلبك فيه منتج شحنه مجاني', self.client.get('/cart').get_data(as_text=True))
        quote = self.client.get('/api/shipping-quote?governorate=القاهرة&city=القاهرة&area=x').json
        self.assertEqual(quote['shipping_piastres'], 0)
        self.assertEqual(quote['products_total_piastres'], 49900 + 10000)
        self.assertEqual(self.checkout().status_code, 302)
        order, items = self.last_order()
        self.assertEqual((order['shipping_fee'], order['total']), (0, 59900))
        self.assertEqual(sorted(i['unit_price'] for i in items), [10000, 49900])

    def test_order_without_offer_product_still_pays_shipping(self):
        self.add(self.mug)
        self.checkout()
        order, _ = self.last_order()
        self.assertEqual((order['shipping_fee'], order['total']), (6000, 16000))

    def test_bundle_discount_counts_only_regular_pieces(self):
        brush = {'slug': OFFER_SLUG, 'price': 30000, 'supplier_cost': 25000, 'source': 'safka'}
        mug = {'slug': 'mug', 'price': 10000, 'supplier_cost': 6000, 'source': 'safka'}
        with patch.dict(os.environ, {'SHOPY_BUNDLE_OFFER_ENABLED': '1'}), store.db() as conn:
            only_brush = checkout_offer(conn, [(brush, 2, {}, 'b')], (7000, 6000))
            self.assertEqual((only_brush['bundle_discount'], only_brush['bundle_unit_count'], only_brush['fee']), (0, 0, 0))
            self.assertEqual(only_brush['merchandise_total'], 2 * 49900)
            brush_and_mug = checkout_offer(conn, [(brush, 1, {}, 'b'), (mug, 1, {}, 'm')], (7000, 6000))
            self.assertEqual((brush_and_mug['bundle_discount'], brush_and_mug['fee']), (0, 0))
            self.assertEqual(brush_and_mug['displayed_subtotal'], 49900 + 19500)
            brush_and_two_mugs = checkout_offer(conn, [(brush, 1, {}, 'b'), (mug, 2, {}, 'm')], (7000, 6000))
            self.assertEqual(brush_and_two_mugs['bundle_discount'], 9500)
            self.assertEqual(brush_and_two_mugs['bundle_unit_count'], 2)

    def test_free_shipping_requires_positive_merchant_contribution(self):
        product = {'slug': OFFER_SLUG, 'price': 30000, 'supplier_cost': 42000, 'source': 'safka'}
        with store.db() as conn:
            offer = checkout_offer(conn, [(product, 1, {}, 'k')], (7000, 6000))
            self.assertEqual((offer['fee'], offer['discount'], offer['free_shipping']), (0, 7000, True))
            self.assertEqual(offer['floor'], MIN_BUNDLE_CONTRIBUTION_PIASTRES)
            with self.assertRaises(ValueError):  # 499 - 440 - 60 < 1 EGP
                checkout_offer(conn, [(dict(product, supplier_cost=44000), 1, {}, 'k')], (7000, 6000))
            with self.assertRaises(ValueError):  # unknown Safka cost never funds free delivery
                checkout_offer(conn, [(dict(product, supplier_cost=None), 1, {}, 'k')], (7000, 6000))

    # --- Safka: sync + order snapshot end to end --------------------------
    def safka_setup(self):
        with store.db() as conn:
            import_products(conn, [safka_record(BRUSH_ID, 'فرشاة الأسنان الاوتوماتيكية', 300),
                                   safka_record(OTHER_ID, 'منتج صفقة تاني', 100)])
            conn.execute("DELETE FROM products WHERE slug=? AND source!='safka'", (OFFER_SLUG,))
            conn.execute('UPDATE products SET slug=? WHERE supplier_product_id=?', (OFFER_SLUG, BRUSH_ID))
            conn.execute('''INSERT INTO safka_shipping_rates(governorate,city,area,supplier_fee,fee,updated_at,pricing_document_id)
                            VALUES(?,?,?,?,?,?,?)''', ('القاهرة', '', '', 6000, 7000, datetime.now(timezone.utc).isoformat(), 'doc-1'))
            ids = {r['supplier_product_id']: r['id'] for r in
                   conn.execute("SELECT id,supplier_product_id FROM products WHERE source='safka'")}
        return ids[BRUSH_ID], ids[OTHER_ID]

    def dry_run_last_order(self):
        order, _ = self.last_order()
        with store.db() as conn:
            return order, prepare_dry_run(conn, order['id'])

    def test_safka_order_with_offer_passes_dry_run_reconciliation(self):
        brush, other = self.safka_setup()
        self.add(brush)
        self.add(other)
        self.assertEqual(self.checkout().status_code, 302)
        order, preview = self.dry_run_last_order()
        self.assertEqual(order['shipping_fee'], 0)
        self.assertEqual(preview['customer_shipping_piastres'], 0)
        self.assertEqual(preview['total_to_collect_piastres'], 49900 + 14000)
        # Commission = what the customer pays minus Safka goods and Safka delivery.
        self.assertEqual(preview['estimated_margin_before_costs_piastres'], 49900 + 14000 - 30000 - 10000 - 6000)

    def test_safka_order_with_offer_and_bundle_passes_dry_run(self):
        brush, other = self.safka_setup()
        with patch.dict(os.environ, {'SHOPY_BUNDLE_OFFER_ENABLED': '1'}):
            self.add(brush)
            self.add(other, 2)
            self.assertEqual(self.checkout().status_code, 302)
            order, preview = self.dry_run_last_order()
        self.assertEqual((order['shipping_fee'], order['bundle_discount'], order['bundle_unit_count']), (0, 9500, 2))
        self.assertEqual(preview['total_to_collect_piastres'], 49900 + 2 * 23500 - 9500)

    def test_tampered_offer_order_fails_dry_run(self):
        brush, _ = self.safka_setup()
        self.add(brush)
        self.checkout()
        order, _ = self.last_order()
        with store.db() as conn:
            conn.execute('UPDATE orders SET shipping_fee=7000,total=total+7000,shipping_discount=0 WHERE id=?', (order['id'],))
            with self.assertRaises(ValueError):
                prepare_dry_run(conn, order['id'])

    def test_safka_sync_cannot_undo_offer(self):
        brush, _ = self.safka_setup()
        with store.db() as conn:
            import_products(conn, [safka_record(BRUSH_ID, 'فرشاة الأسنان الاوتوماتيكية', 320)])
            row = conn.execute('SELECT * FROM products WHERE id=?', (brush,)).fetchone()
        self.assertEqual(row['slug'], OFFER_SLUG)
        self.assertEqual(row['supplier_cost'], 32000)
        self.assertEqual(displayed_product_price(row), 49900)
        self.assertEqual(product_sale_badge(row)['percent'], 50)

    def test_snapshot_validator_rules(self):
        raw = {'bundle_offer_version': 0, 'shipping_standard_fee': 7000, 'supplier_shipping_piastres': 6000,
               'customer_shipping_piastres': 0, 'shipping_discount': 7000, 'retained_profit_floor': 100,
               'free_shipping_applied': 1}
        validate_delivery_snapshot(raw, 49900, 30000, quantity=1)
        with self.assertRaises(ValueError):  # goods no longer cover supplier goods + delivery
            validate_delivery_snapshot(raw, 49900, 44000, quantity=1)
        with self.assertRaises(ValueError):  # without the offer, a full discount needs the 15% margin rule
            validate_delivery_snapshot(dict(raw, free_shipping_applied=0), 49900, 40000, quantity=1)
        v3 = dict(raw, bundle_offer_version=3, bundle_discount=0, bundle_unit_count=0)
        validate_delivery_snapshot(v3, 49900, 30000, quantity=1, bundle_quantity=0)
        with self.assertRaises(ValueError):  # offer-only order can't claim a bundle discount
            validate_delivery_snapshot(dict(v3, bundle_discount=9500, bundle_unit_count=2), 49900, 30000,
                                       quantity=1, bundle_quantity=0)

    # --- admin ------------------------------------------------------------
    def test_admin_navigation_on_admin_pages_only(self):
        self.client.post('/login', data={'email': 'owner@example.test',
                                          'password': 'strong-owner-password', 'csrf': self.token()})
        for path in ('/admin', '/admin/promo-codes', '/admin/pricing'):
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn('class="admin-sidebar"', html)
                self.assertIn('action="/logout"', html)
        admin = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('عرض خاص ثابت في الكود', admin)
        self.assertNotIn('compare_at_price', admin)  # no DB-backed offer fields any more
        self.assertNotIn('class="admin-sidebar"', self.client.get('/').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
