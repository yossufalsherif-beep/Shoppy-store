"""Regression tests for Arabic product URL decoding behind a reverse proxy.

Strict exact-slug lookups only. No production data or network access.
"""
import unittest
from urllib.parse import quote
from werkzeug.exceptions import NotFound
from tests.test_store import StoreTests
import app as store

class ArabicProductRouteTests(unittest.TestCase):
    def setUp(self):
        StoreTests.setUp(self)
        with store.db() as conn:
            conn.execute("INSERT INTO products(title,slug,description,price,active) VALUES (?,?,?,?,1)",
                         ('منتج تجريبي','شنطة-اختبار-عربي','منتج اختبار',23000))
        self.slug='شنطة-اختبار-عربي'

    def tearDown(self):
        StoreTests.tearDown(self)

    def test_direct_arabic_url_renders_product(self):
        page=self.client.get('/products/'+quote(self.slug))
        self.assertEqual(page.status_code,200)
        self.assertIn(self.slug.encode('utf-8'),page.data)

    def test_proxy_encoded_slug_is_recovered_by_exact_match(self):
        encoded=quote(self.slug)
        variants=store.product_slug_variants(encoded)
        self.assertIn(self.slug,variants)
        with store.app.test_request_context('/products/'+encoded):
            page=store.product(encoded)
            self.assertIn(self.slug,page)

    def test_double_encoded_slug_is_supported(self):
        double=quote(quote(self.slug))
        with store.app.test_request_context('/products/'+double):
            page=store.product(double)
            self.assertIn(self.slug,page)

    def test_latin1_mojibake_is_recovered_without_guessing(self):
        mojibake=self.slug.encode('utf-8').decode('latin-1')
        self.assertIn(self.slug,store.product_slug_variants(mojibake))
        with store.app.test_request_context('/products/'+quote(self.slug)):
            page=store.product(mojibake)
            self.assertIn(self.slug,page)

    def test_no_matching_product_still_404(self):
        with store.app.test_request_context('/products/%D8%A7%D8%B3%D9%85'):
            with self.assertRaises(NotFound):
                store.product('%D8%A7%D8%B3%D9%85')

    def test_no_slash_path_traversal_or_unbounded_decoding(self):
        self.assertNotIn('../admin',store.product_slug_variants('..%2Fadmin'))
        self.assertEqual(store.product_slug_variants('%ZZ'),('%ZZ',))
        self.assertLessEqual(len(store.product_slug_variants('%25'*120)),3)

if __name__=='__main__':
    unittest.main()
