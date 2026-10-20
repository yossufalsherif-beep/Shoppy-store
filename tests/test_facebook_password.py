"""Offline tests for Facebook OAuth rollout and login/register password visibility."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as store
import social_auth as social

CONFIG={
    'SHOPY_AUTH_SUPABASE_URL':'https://example.supabase.co',
    'SHOPY_AUTH_PUBLISHABLE_KEY':'sb_publishable_offline',
    'SHOPY_PUBLIC_URL':'https://shop.example',
    'SHOPY_AUTH_GOOGLE_ENABLED':'1',
    'SHOPY_AUTH_FACEBOOK_ENABLED':'1',
}
FACEBOOK_USER={
    'id':'supabase-test-user',
    'email':'facebook-user@example.test',
    'email_confirmed_at':'2026-10-20T00:00:00Z',
    'is_anonymous':False,
    'identities':[{'provider':'facebook','identity_data':{'sub':'stable-facebook-subject'}}],
}

class FacebookPasswordTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.dbpatch=patch.object(store,'DB',str(Path(self.temp.name)/'test.sqlite'))
        self.dbpatch.start()
        self.env=patch.dict(os.environ,CONFIG)
        self.env.start()
        store.init()
        store.app.config.update(TESTING=True,SECRET_KEY='facebook-offline-tests')
        self.client=store.app.test_client()
        with self.client.session_transaction() as sess:
            sess['csrf']='facebook-test-csrf'

    def tearDown(self):
        self.env.stop()
        self.dbpatch.stop()
        self.temp.cleanup()

    def test_both_auth_pages_have_password_visibility_toggle(self):
        for path in ('/login','/register'):
            with self.subTest(path=path):
                page=self.client.get(path)
                self.assertEqual(page.status_code,200)
                html=page.data.decode()
                self.assertIn('data-toggle-password="auth-password"',html)
                self.assertIn('type="password" name="password"',html)
                self.assertIn('type="button" data-toggle-password',html)
                self.assertIn('aria-pressed="false"',html)
                self.assertIn('إظهار كلمة المرور',html)
                self.assertIn('المتابعة باستخدام Facebook',html)
                self.assertIn('action="/auth/facebook/start"',html)

    def test_disabled_facebook_cannot_start_oauth(self):
        with patch.dict(os.environ,{'SHOPY_AUTH_FACEBOOK_ENABLED':'0'}):
            page=self.client.get('/login').data.decode()
            self.assertIn('social-facebook" disabled',page)
            self.assertIn('غير مفعّل بعد',page)
            with patch('social_auth.auth_request') as remote:
                resp=self.client.post('/auth/facebook/start',data={'csrf':'facebook-test-csrf'})
            remote.assert_not_called()
            self.assertEqual(resp.location,'/login')

    def test_facebook_requires_actual_supabase_provider_enabled(self):
        with patch('social_auth.auth_request',return_value={'external':{'google':True,'facebook':False}}):
            resp=self.client.post('/auth/facebook/start',data={'csrf':'facebook-test-csrf'})
        self.assertEqual(resp.status_code,302)
        self.assertEqual(resp.location,'/login')
        with self.client.session_transaction() as sess:
            self.assertNotIn('oauth_flow',sess)

    def test_facebook_flow_uses_pkce_and_provider_identity(self):
        with patch('social_auth.auth_request',return_value={'external':{'google':True,'facebook':True}}):
            start=self.client.post('/auth/facebook/start',data={'csrf':'facebook-test-csrf'})
        self.assertEqual(start.status_code,302)
        self.assertIn('provider=facebook',start.location)
        self.assertIn('code_challenge_method=s256',start.location)
        with self.client.session_transaction() as sess:
            flow=dict(sess['oauth_flow'])
        self.assertEqual(flow['provider'],'facebook')
        self.assertNotIn(flow['verifier'],start.location)
        with patch('social_auth.auth_request',side_effect=[
            {'access_token':'server-verified-only'},
            FACEBOOK_USER,
        ]):
            callback=self.client.get('/auth/callback',query_string={
                'flow':flow['state'],'code':'valid-pkce-code',
            })
        self.assertEqual(callback.status_code,302)
        self.assertEqual(callback.location,'/account')
        with store.db() as conn:
            user=conn.execute('SELECT email,role FROM users').fetchone()
            self.assertEqual(user['email'],FACEBOOK_USER['email'])
            self.assertEqual(user['role'],'customer')
            link=conn.execute('SELECT provider,subject FROM social_identities').fetchone()
            self.assertEqual((link['provider'],link['subject']),('facebook','stable-facebook-subject'))
        with self.client.session_transaction() as sess:
            self.assertNotIn('oauth_flow',sess)
            self.assertNotIn('server-verified-only',str(dict(sess)))

    def test_unknown_provider_is_still_rejected(self):
        self.assertEqual(self.client.post('/auth/apple/start',data={'csrf':'facebook-test-csrf'}).status_code,404)

    def test_password_toggle_does_not_change_server_validation(self):
        page=self.client.get('/register').data.decode()
        self.assertIn('minlength="10" required',page)
        self.assertIn('autocomplete="new-password"',page)
        page=self.client.get('/login').data.decode()
        self.assertIn('autocomplete="current-password"',page)

if __name__=='__main__':
    unittest.main()
