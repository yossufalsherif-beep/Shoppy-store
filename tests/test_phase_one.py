"""Phase-one guest checkout, opt-in funnel privacy, and saved phone tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as store


class PhaseOneTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db_override=patch.object(store,'DB',str(Path(self.temp.name)/'phase-one.sqlite3'))
        self.db_override.start()
        store.init()
        store.app.config.update(TESTING=True,SECRET_KEY='offline-phase-one-key')
        self.client=store.app.test_client()
        with store.db() as conn:
            conn.execute("""INSERT INTO products(title,slug,description,price,image,active,options_json,category)
                VALUES(?,?,?,?,?,?,?,?)""",('منتج تجريبي','phase-one-item','وصف',10000,'',1,'{}','اختبار'))
            conn.execute("INSERT INTO shipping(governorate,fee) VALUES('الجيزة',7000)")

    def tearDown(self):
        self.db_override.stop()
        self.temp.cleanup()

    def token(self):
        with self.client.session_transaction() as s:s['csrf']='phase-one-csrf'
        return 'phase-one-csrf'

    def counters(self):
        with store.db() as conn:
            return {r['stage']:r['visits'] for r in conn.execute('SELECT stage,visits FROM analytics_counters')}

    def test_tracking_is_off_by_default_and_after_refusal(self):
        self.client.get('/')
        self.client.get('/products/phase-one-item')
        self.assertEqual(self.client.post('/analytics/event',data={'csrf':self.token(),'stage':'home'}).status_code,403)
        self.assertEqual(self.counters(),{})
        self.assertTrue(self.client.post('/analytics/consent',data={'csrf':self.token(),'choice':'no'}).json['ok'])
        self.assertEqual(self.client.post('/analytics/event',data={'csrf':self.token(),'stage':'home'}).status_code,403)
        self.assertEqual(self.counters(),{})

    def test_optin_deduplicates_per_browser_session(self):
        csrf=self.token()
        self.assertEqual(self.client.post('/analytics/consent',data={'csrf':csrf,'choice':'yes'}).status_code,200)
        self.assertEqual(self.client.post('/analytics/event',data={'csrf':csrf,'stage':'home'}).status_code,200)
        self.assertEqual(self.client.post('/analytics/event',data={'csrf':csrf,'stage':'home'}).status_code,200)
        self.assertEqual(self.counters(),{'home':1})
        self.assertEqual(self.client.post('/analytics/event',data={'csrf':csrf,'stage':'arbitrary'}).status_code,400)
        self.assertEqual(self.client.post('/analytics/consent',data={'csrf':'wrong','choice':'no'}).status_code,400)
        self.assertEqual(self.counters(),{'home':1})

    def test_customer_phone_is_saved_and_unverified_by_default(self):
        response=self.client.post('/register',data={'csrf':self.token(),
            'email':'buyer@test.example','password':'strong-test-password',
            'phone':'01012345678'})
        self.assertEqual(response.status_code,302)
        with store.db() as conn:
            user=conn.execute("SELECT phone FROM users WHERE email='buyer@test.example'").fetchone()
            self.assertEqual(user['phone'],'01012345678')
        checkout=self.client.get('/account')
        self.assertIn(b'01012345678',checkout.data)
        invalid=self.client.post('/account/phone',data={'csrf':self.token(),'phone':'bad'})
        self.assertEqual(invalid.status_code,302)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT phone FROM users').fetchone()['phone'],'01012345678')
        update=self.client.post('/account/phone',data={'csrf':self.token(),'phone':'01112345678'})
        self.assertEqual(update.status_code,302)
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT phone FROM users').fetchone()['phone'],'01112345678')

    def test_guest_checkout_writes_once_and_reference_does_not_authenticate_others(self):
        csrf=self.token()
        self.assertEqual(self.client.post('/cart/add/1',data={'csrf':csrf}).status_code,302)
        payload={'csrf':csrf,'name':'عميل اختبار','phone':'01012345678',
                 'governorate':'الجيزة','city':'6 أكتوبر','area':'الحي الأول',
                 'address':'شارع اختبار 12 عمارة 4'}
        done=self.client.post('/checkout',data=payload)
        self.assertEqual(done.status_code,302)
        self.assertIn('/order/success/',done.location)
        url=done.location
        self.assertEqual(self.client.get(url).status_code,200)
        with store.db() as conn:
            row=conn.execute('SELECT user_id,total FROM orders').fetchone()
            self.assertIsNone(row['user_id'])
            self.assertEqual(row['total'],17000)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0],1)
        other=store.app.test_client()
        self.assertEqual(other.get(url).status_code,404)
        self.assertEqual(self.client.get('/admin/funnel').status_code,302)


if __name__=='__main__':
    unittest.main()
