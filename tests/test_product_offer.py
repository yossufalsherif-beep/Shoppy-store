"""Per-product sale offer (compare-at price + badge), free shipping and quick add."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as store
from werkzeug.security import generate_password_hash
from profit_pricing import (checkout_offer, product_sale_badge, validate_delivery_snapshot,
                            MIN_BUNDLE_CONTRIBUTION_PIASTRES)
from safka_bridge import import_products


SAFKA_ID = '673a2c721548cec72da05e99'


def safka_record(price=300):
    return {'_id': SAFKA_ID, 'name': 'فرشاة الأسنان الاوتوماتيكية', 'barcode': 'TB1',
            'sale_price': price, 'images': ['https://example.com/brush.jpg'],
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
            conn.execute('INSERT INTO shipping(governorate,fee) VALUES(?,?)', ('القاهرة', 6000))
            conn.execute('INSERT INTO products(title,slug,description,price) VALUES(?,?,?,?)',
                         ('فرشاة أسنان', 'brush', 'وصف', 99900))
            conn.execute('INSERT INTO products(title,slug,description,price) VALUES(?,?,?,?)',
                         ('كوب', 'mug', 'وصف', 10000))
            conn.execute("INSERT INTO products(title,slug,description,price,options_json) VALUES(?,?,?,?,?)",
                         ('تيشيرت', 'tee', 'وصف', 20000, '{"المقاس":["M","L"]}'))
            self.brush = conn.execute("SELECT id FROM products WHERE slug='brush'").fetchone()['id']
            self.mug = conn.execute("SELECT id FROM products WHERE slug='mug'").fetchone()['id']
            self.tee = conn.execute("SELECT id FROM products WHERE slug='tee'").fetchone()['id']

    def tearDown(self):
        self.database_patch.stop()
        self.temp.cleanup()
        self.env.stop()

    def token(self):
        with self.client.session_transaction() as sess:
            sess['csrf'] = 'csrf-test-token'
        return 'csrf-test-token'

    def login(self):
        self.client.post('/login', data={'email': 'owner@example.test',
                                          'password': 'strong-owner-password', 'csrf': self.token()})

    def set_offer(self, pid, price='499', compare='999', free='1', title='فرشاة أسنان'):
        data = {'csrf': self.token(), 'title': title, 'description': 'وصف', 'category': '',
                'price': price, 'compare_at_price': compare}
        if free:
            data['free_shipping'] = free
        return self.client.post('/admin/product/%d/edit' % pid, data=data)

    def checkout(self):
        return self.client.post('/checkout', data={
            'csrf': self.token(), 'name': 'عميل', 'phone': '01012345678', 'governorate': 'القاهرة',
            'city': 'القاهرة', 'area': 'مدينة نصر', 'address': 'شارع عشرة عمارة 4'})

    # --- admin + display -------------------------------------------------
    def test_admin_sets_offer_and_badge_shows_on_card_and_product_page(self):
        self.login()
        self.assertEqual(self.set_offer(self.brush).status_code, 302)
        with store.db() as conn:
            row = conn.execute('SELECT price,compare_at_price,free_shipping FROM products WHERE id=?',
                               (self.brush,)).fetchone()
        self.assertEqual((row['price'], row['compare_at_price'], row['free_shipping']), (49900, 99900, 1))
        home = self.client.get('/').get_data(as_text=True)
        self.assertIn('خصم 50%', home)
        self.assertIn('<s class="compare-price"', home)
        self.assertIn('999.00', home)
        self.assertIn('شحن مجاني', home)
        page = self.client.get('/products/brush').get_data(as_text=True)
        self.assertIn('sale-badge-large', page)
        self.assertIn('خصم 50%', page)
        self.assertIn('499.00', page)
        self.assertIn('شحن مجاني', page)
        admin = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('name="compare_at_price"', admin)
        self.assertIn('name="free_shipping"', admin)

    def test_compare_at_must_exceed_price_and_can_be_cleared(self):
        self.login()
        self.set_offer(self.brush, price='499', compare='400')
        with store.db() as conn:
            row = conn.execute('SELECT price,compare_at_price FROM products WHERE id=?', (self.brush,)).fetchone()
        self.assertEqual((row['price'], row['compare_at_price']), (99900, None))  # rejected, unchanged
        self.set_offer(self.brush)
        self.set_offer(self.brush, compare='', free='')
        with store.db() as conn:
            row = conn.execute('SELECT compare_at_price,free_shipping FROM products WHERE id=?', (self.brush,)).fetchone()
        self.assertEqual((row['compare_at_price'], row['free_shipping']), (None, 0))
        self.assertNotIn('خصم 50%', self.client.get('/').get_data(as_text=True))

    def test_badge_rounds_down_and_ignores_invalid_values(self):
        self.assertEqual(product_sale_badge({'price': 49900, 'compare_at_price': 99900}),
                         {'compare_at': 99900, 'percent': 50})
        self.assertIsNone(product_sale_badge({'price': 49900, 'compare_at_price': 49900}))
        self.assertIsNone(product_sale_badge({'price': 49900, 'compare_at_price': None}))
        self.assertIsNone(product_sale_badge({'price': 49900}))  # pre-migration row

    # --- quick add --------------------------------------------------------
    def test_quick_add_overlay_on_cards(self):
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('class="cover-cart-form" method="post" action="/cart/add/%d" data-async="cart"' % self.mug, html)
        self.assertIn('aria-label="أضف للسلة: كوب"', html)
        # Products with options link to the product page instead of adding blindly.
        self.assertIn('<a class="cover-cart-button" href="/products/tee" aria-label="اختار المواصفات وأضف للسلة: تيشيرت"', html)
        self.assertNotIn('class="cover-cart-form" method="post" action="/cart/add/%d"' % self.tee, html)
        search = self.client.get('/?q=كوب').get_data(as_text=True)
        self.assertIn('aria-label="أضف للسلة: كوب"', search)

    def test_quick_add_json_updates_count_and_html_returns_to_page(self):
        response = self.client.post('/cart/add/%d' % self.mug, data={'csrf': self.token(), 'return_to': '/?q=x'},
                                    headers={'Accept': 'application/json'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['cart_count'], 1)
        response = self.client.post('/cart/add/%d' % self.mug, data={'csrf': self.token(), 'return_to': '/?q=x'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/?q=x'))
        self.assertEqual(self.client.post('/cart/add/%d' % self.mug, data={'csrf': 'bad'}).status_code, 400)

    # --- free shipping at checkout ----------------------------------------
    def test_free_shipping_product_makes_whole_order_ship_free(self):
        self.login()
        self.set_offer(self.brush)
        self.client.post('/cart/add/%d' % self.brush, data={'csrf': self.token()})
        self.client.post('/cart/add/%d' % self.mug, data={'csrf': self.token()})
        cart = self.client.get('/cart').get_data(as_text=True)
        self.assertIn('مجاني — طلبك فيه منتج شحنه مجاني', cart)
        quote = self.client.get('/api/shipping-quote?governorate=القاهرة&city=القاهرة&area=x').json
        self.assertEqual(quote['shipping_piastres'], 0)
        self.assertEqual(self.checkout().status_code, 302)
        with store.db() as conn:
            order = conn.execute('SELECT * FROM orders').fetchone()
        self.assertEqual(order['shipping_fee'], 0)
        self.assertEqual(order['total'], 49900 + 10000)
        self.assertEqual(order['free_shipping_applied'], 1)

    def test_order_without_free_product_still_pays_shipping(self):
        self.client.post('/cart/add/%d' % self.mug, data={'csrf': self.token()})
        self.checkout()
        with store.db() as conn:
            order = conn.execute('SELECT * FROM orders').fetchone()
        self.assertEqual((order['shipping_fee'], order['free_shipping_applied']), (6000, 0))

    def test_free_shipping_requires_positive_merchant_contribution(self):
        product = {'price': 49900, 'supplier_cost': 42000, 'source': 'safka', 'free_shipping': 1}
        with store.db() as conn:
            offer = checkout_offer(conn, [(product, 1, {}, 'k')], (7000, 6000))
            self.assertEqual((offer['fee'], offer['discount'], offer['free_shipping']), (0, 7000, True))
            self.assertEqual(offer['floor'], MIN_BUNDLE_CONTRIBUTION_PIASTRES)
            with self.assertRaises(ValueError):  # 499 - 440 - 60 < 1 EGP
                checkout_offer(conn, [(dict(product, supplier_cost=44000), 1, {}, 'k')], (7000, 6000))
            with self.assertRaises(ValueError):  # unknown Safka cost never funds free delivery
                checkout_offer(conn, [(dict(product, supplier_cost=None), 1, {}, 'k')], (7000, 6000))

    def test_free_shipping_with_bundle_offer_single_item(self):
        product = {'price': 49900, 'supplier_cost': 30000, 'source': 'safka', 'free_shipping': 1}
        with patch.dict(os.environ, {'SHOPY_BUNDLE_OFFER_ENABLED': '1'}):
            with store.db() as conn:
                offer = checkout_offer(conn, [(product, 1, {}, 'k')], (7000, 6000))
        self.assertEqual((offer['fee'], offer['bundle_offer_version'], offer['free_shipping']), (0, 3, True))

    # --- Safka order snapshot reconciliation ------------------------------
    def snapshot(self, **changes):
        raw = {'bundle_offer_version': 0, 'shipping_standard_fee': 7000, 'supplier_shipping_piastres': 6000,
               'customer_shipping_piastres': 0, 'shipping_discount': 7000, 'retained_profit_floor': 100,
               'free_shipping_applied': 1}
        raw.update(changes)
        return raw

    def test_dry_run_snapshot_accepts_free_shipping_and_rejects_tampering(self):
        validate_delivery_snapshot(self.snapshot(), 49900, 30000, quantity=1)
        for bad in ({'customer_shipping_piastres': 7000, 'shipping_discount': 0},  # charged anyway
                    {'free_shipping_applied': 2},
                    {'retained_profit_floor': 0}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_delivery_snapshot(self.snapshot(**bad), 49900, 30000, quantity=1)
        with self.assertRaises(ValueError):  # goods no longer cover supplier goods + delivery
            validate_delivery_snapshot(self.snapshot(), 49900, 44000, quantity=1)
        # Without the flag, a full discount still needs the legacy 15% margin rule.
        with self.assertRaises(ValueError):
            validate_delivery_snapshot(self.snapshot(free_shipping_applied=0), 49900, 40000, quantity=1)
        v3 = self.snapshot(bundle_offer_version=3, bundle_discount=0, bundle_unit_count=1)
        validate_delivery_snapshot(v3, 49900, 30000, quantity=1)

    # --- supplier sync keeps manual values --------------------------------
    def test_safka_sync_does_not_overwrite_offer(self):
        with store.db() as conn:
            import_products(conn, [safka_record()])
            pid = conn.execute("SELECT id FROM products WHERE supplier_product_id=?", (SAFKA_ID,)).fetchone()['id']
        self.login()
        self.set_offer(pid, title='فرشاة الأسنان الاوتوماتيكية')
        with store.db() as conn:
            import_products(conn, [safka_record(price=310)])
            row = conn.execute('SELECT price,compare_at_price,free_shipping,supplier_cost FROM products WHERE id=?',
                               (pid,)).fetchone()
        self.assertEqual((row['price'], row['compare_at_price'], row['free_shipping']), (49900, 99900, 1))
        self.assertEqual(row['supplier_cost'], 31000)

    def test_admin_navigation_on_admin_pages_only(self):
        self.login()
        for path in ('/admin', '/admin/promo-codes', '/admin/pricing'):
            with self.subTest(path=path):
                html = self.client.get(path).get_data(as_text=True)
                self.assertIn('class="admin-sidebar"', html)
                self.assertIn('action="/logout"', html)
        self.assertNotIn('class="admin-sidebar"', self.client.get('/').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
