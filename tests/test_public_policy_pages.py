"""Public policy links and transparent account removal instructions."""
import unittest
from tests.test_store import StoreTests
import app as store

class PublicPolicyTests(unittest.TestCase):
    def setUp(self):
        StoreTests.setUp(self)

    def tearDown(self):
        StoreTests.tearDown(self)

    def test_privacy_is_public_and_has_expected_privacy_disclosures(self):
        page=self.client.get('/privacy')
        self.assertEqual(page.status_code,200)
        self.assertIn('سياسة الخصوصية',page.data.decode('utf-8'))
        self.assertIn('Google',page.data.decode('utf-8'))
        self.assertIn('Facebook',page.data.decode('utf-8'))
        self.assertIn('Supabase',page.data.decode('utf-8'))
        self.assertIn('سجلات الطلبات',page.data.decode('utf-8'))
        self.assertIn('/data-deletion',page.data.decode('utf-8'))
        self.assertNotIn('name="csrf"',page.data.decode('utf-8'))

    def test_data_removal_instructions_are_public_and_accurate(self):
        page=self.client.get('/data-deletion')
        self.assertEqual(page.status_code,200)
        html=page.data.decode('utf-8')
        self.assertIn('حذف حسابك وبياناتك',html)
        self.assertIn('href="/login"',html)
        self.assertIn('href="/account"',html)
        self.assertIn('href="/privacy"',html)
        self.assertIn('Facebook',html)
        self.assertIn('Supabase',html)
        self.assertIn('المحاسبة',html)
        self.assertIn('href="/#contact"',html)
        self.assertNotIn('method="post" action="/account/delete"',html)

    def test_every_public_page_links_to_policy_and_removal_instructions(self):
        for path in ('/','/login','/register','/privacy','/data-deletion'):
            with self.subTest(path=path):
                page=self.client.get(path)
                self.assertEqual(page.status_code,200)
                html=page.data.decode('utf-8')
                self.assertIn('<a href="/privacy">سياسة الخصوصية</a>',html)
                self.assertIn('<a href="/data-deletion">حذف حسابك وبياناتك</a>',html)

if __name__=='__main__':
    unittest.main()
