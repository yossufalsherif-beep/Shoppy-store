"""OAuth identity binding and PKCE callbacks; provider calls are mocked."""
import os,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from werkzeug.security import generate_password_hash
import app as store
import social_auth as social

ENV={'SHOPY_AUTH_SUPABASE_URL':'https://example.supabase.co','SHOPY_AUTH_PUBLISHABLE_KEY':'sb_publishable_offline',
     'SHOPY_PUBLIC_URL':'https://shop.example','SHOPY_AUTH_GOOGLE_ENABLED':'1','SHOPY_AUTH_APPLE_ENABLED':'1'}
USER={'id':'auth-user-id','email':'new@example.test','email_confirmed_at':'2026-10-19','is_anonymous':False,
      'user_metadata':{'role':'admin'},'identities':[{'provider':'google','identity_data':{'sub':'immutable-google-subject'}}]}

class SocialFlowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.dbpatch=patch.object(store,'DB',str(Path(self.tmp.name)/'auth.sqlite'));self.dbpatch.start()
  self.env=patch.dict(os.environ,ENV);self.env.start();store.init()
  store.app.config.update(TESTING=True,SECRET_KEY='social-test-only');self.client=store.app.test_client()
  with self.client.session_transaction() as s:s['csrf']='test-csrf'
 def tearDown(self):self.env.stop();self.dbpatch.stop();self.tmp.cleanup()
 def start(self,provider='google'):
  with patch('social_auth.auth_request',return_value={'external':{'google':True,'apple':True}}):
   response=self.client.post('/auth/'+provider+'/start',data={'csrf':'test-csrf'})
  self.assertEqual(response.status_code,302)
  with self.client.session_transaction() as s:self.flow=dict(s['oauth_flow'])
  return response
 def test_start_requires_csrf_and_has_pkce(self):
  self.assertEqual(self.client.post('/auth/google/start').status_code,400)
  response=self.start();self.assertIn('code_challenge_method=s256',response.location)
  self.assertNotIn(self.flow['verifier'],response.location)
 def test_signup_verifies_server_user_and_never_uses_profile_role(self):
  self.start()
  with patch('social_auth.auth_request',side_effect=[{'access_token':'never-store-this-token'},USER]) as api:
   response=self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'provider-code'})
  self.assertEqual(response.location,'/account')
  with store.db() as c:
   self.assertEqual(c.execute('SELECT role FROM users').fetchone()[0],'customer')
  with self.client.session_transaction() as s:
   self.assertNotIn('oauth_flow',s);self.assertNotIn('never-store-this-token',str(dict(s)))
  self.assertEqual(api.call_args_list[1].args,('user',))
 def test_wrong_browser_expired_and_replay_callbacks_cannot_sign_in(self):
  self.start()
  with patch('app.exchange_identity') as exchange:
   self.client.get('/auth/callback?flow=wrong&code=x');exchange.assert_not_called()
   with self.client.session_transaction() as s:
    flow=dict(s['oauth_flow']);flow['created']=time.time()-601;s['oauth_flow']=flow
   self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'x'});exchange.assert_not_called()
  self.start()
  with patch('social_auth.auth_request',side_effect=[{'access_token':'test'},USER]):
   self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'x'})
  with patch('app.exchange_identity') as exchange:
   self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'x'});exchange.assert_not_called()
 def test_same_email_never_grants_existing_admin_access(self):
  with store.db() as c:c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',(USER['email'],generate_password_hash('owner-long-password'),'admin'))
  self.start()
  with patch('social_auth.auth_request',side_effect=[{'access_token':'test'},USER]):
   response=self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'x'})
  self.assertEqual(response.location,'/login')
  with self.client.session_transaction() as s:self.assertNotIn('uid',s)
  with store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM social_identities').fetchone()[0],0)
 def test_unverified_email_cannot_create_customer(self):
  self.start();bad={**USER,'email_confirmed_at':None}
  with patch('social_auth.auth_request',side_effect=[{'access_token':'test'},bad]):
   self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'x'})
  with store.db() as c:self.assertEqual(c.execute('SELECT count(*) FROM users').fetchone()[0],0)
 def test_explicit_customer_link_preserves_account_and_existing_identity(self):
  with store.db() as c:uid=c.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',('old@example.test',generate_password_hash('safe-long-password'),'customer')).lastrowid
  with self.client.session_transaction() as s:s['uid']=uid
  self.start()
  with patch('social_auth.auth_request',side_effect=[{'access_token':'test'},USER]):
   self.client.get('/auth/callback',query_string={'flow':self.flow['state'],'code':'x'})
  with store.db() as c:
   self.assertEqual(c.execute('SELECT count(*) FROM users').fetchone()[0],1)
   self.assertEqual(c.execute('SELECT user_id FROM social_identities').fetchone()[0],uid)
 def test_apple_provider_uses_same_pkce_callback_without_relaxing_cookie_policy(self):
  response=self.start('apple');self.assertIn('provider=apple',response.location)
  self.assertEqual(store.app.config['SESSION_COOKIE_SAMESITE'],'Lax')
 def test_provider_cannot_be_enabled_by_flag_without_actual_configuration(self):
  with patch('social_auth.auth_request',return_value={'external':{'google':False}}):
   response=self.client.post('/auth/google/start',data={'csrf':'test-csrf'})
  self.assertEqual(response.location,'/login')
  with self.client.session_transaction() as s:self.assertNotIn('oauth_flow',s)
