"""Supabase OAuth PKCE bridge for existing Flask customer sessions.

Supabase handles provider state, OIDC signatures and Apple's form_post callback.
This server exchanges only a code bound to this browser's verifier, verifies the
user through /user, then maps an immutable provider subject to a local account.
Provider tokens never enter the Flask session or HTML.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import time
import urllib.request
from urllib.parse import urlencode, urlsplit

PROVIDERS = ('google', 'apple')

class SocialAuthError(ValueError):
    pass


def init_social_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS social_identities(
        provider TEXT NOT NULL,subject TEXT NOT NULL,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        auth_user_id TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(provider,subject),UNIQUE(user_id,provider))''')
    if os.getenv('DATABASE_URL'):
        conn.execute('ALTER TABLE social_identities ENABLE ROW LEVEL SECURITY')


def config():
    url=os.getenv('SHOPY_AUTH_SUPABASE_URL','').rstrip('/')
    public=os.getenv('SHOPY_AUTH_PUBLISHABLE_KEY','')
    origin=os.getenv('SHOPY_PUBLIC_URL','').rstrip('/')
    parsed=urlsplit(url)
    site=urlsplit(origin)
    if not (parsed.scheme=='https' and parsed.hostname and parsed.hostname.endswith('.supabase.co')
            and not parsed.path and not parsed.username and not parsed.query and not parsed.fragment
            and site.scheme=='https' and site.netloc and not site.path and not site.username and not site.query and not site.fragment
            and public.startswith('sb_publishable_')):
        return None
    return url,public,origin


def configured_providers():
    ready=config() is not None
    return {p:ready and os.getenv('SHOPY_AUTH_'+p.upper()+'_ENABLED')=='1' for p in PROVIDERS}


def auth_request(path, *, payload=None, access_token=None):
    settings=config()
    if settings is None:raise SocialAuthError('Social authentication is not configured')
    url,key,_=settings
    headers={'apikey':key,'Accept':'application/json'}
    if access_token:headers['Authorization']='Bearer '+access_token
    body=json.dumps(payload).encode() if payload is not None else None
    if body is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(url+'/auth/v1/'+path,data=body,headers=headers)
    try:
        with urllib.request.urlopen(req,timeout=12) as response:
            raw=response.read(1024*1024+1)
            if len(raw)>1024*1024:raise SocialAuthError('Invalid authentication response')
            result=json.loads(raw)
    except (OSError,ValueError,TypeError) as exc:
        raise SocialAuthError('Authentication service is temporarily unavailable') from None
    if not isinstance(result,dict):raise SocialAuthError('Invalid authentication response')
    return result


def start_flow(provider, *, link_uid=None):
    if provider not in PROVIDERS or not configured_providers().get(provider):
        raise SocialAuthError('Provider is not enabled')
    # Check actual provider availability; flags alone are not proof of setup.
    external=auth_request('settings').get('external',{})
    if external.get(provider) is not True:raise SocialAuthError('Provider setup is incomplete')
    verifier=secrets.token_urlsafe(64)
    state=secrets.token_urlsafe(32)
    flow={'provider':provider,'verifier':verifier,'state':state,'created':time.time(),'link_uid':link_uid}
    callback=config()[2]+'/auth/callback?'+urlencode({'flow':state})
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    url=config()[0]+'/auth/v1/authorize?'+urlencode({'provider':provider,'redirect_to':callback,
        'code_challenge':challenge,'code_challenge_method':'s256'})
    return flow,url


def exchange_identity(flow, code):
    if not isinstance(code,str) or not 1<=len(code)<=2048:raise SocialAuthError('Missing authorization code')
    token=auth_request('token?grant_type=pkce',payload={'auth_code':code,'code_verifier':flow['verifier']})
    access=token.get('access_token')
    if not isinstance(access,str) or not access:raise SocialAuthError('Missing authentication token')
    # A server-verified user, never an unsigned JWT or browser-supplied profile.
    user=auth_request('user',access_token=access)
    raw_email=user.get('email')
    email=raw_email.strip().lower() if isinstance(raw_email,str) else ''
    if (not user.get('email_confirmed_at') or user.get('is_anonymous') is True
        or not isinstance(user.get('id'),str) or not user['id']
        or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email)):
        raise SocialAuthError('A verified email is required')
    identity=next((i for i in user.get('identities',[]) if isinstance(i,dict) and i.get('provider')==flow['provider']),None)
    subject=(identity or {}).get('identity_data',{}).get('sub')
    if not isinstance(subject,str) or not 1<=len(subject)<=255:
        raise SocialAuthError('Verified provider identity is missing')
    return {'provider':flow['provider'],'subject':subject,'auth_user_id':user['id'],'email':email}


def resolve_account(conn, identity, *, link_uid=None, password_hash):
    existing=conn.execute('SELECT user_id FROM social_identities WHERE provider=? AND subject=?',
                          (identity['provider'],identity['subject'])).fetchone()
    if existing:
        if link_uid is not None and existing['user_id']!=link_uid:
            raise SocialAuthError('الحساب الخارجي مربوط بحساب آخر بالفعل.')
        return conn.execute('SELECT id,role FROM users WHERE id=?',(existing['user_id'],)).fetchone()
    if link_uid is None:
        if conn.execute('SELECT id FROM users WHERE lower(email)=?',(identity['email'],)).fetchone():
            raise SocialAuthError('البريد له حساب بالفعل؛ ادخل بكلمة المرور ثم اربط الحساب من صفحة حسابي.')
        cur=conn.execute('INSERT INTO users(email,password,role) VALUES(?,?,?)',
                         (identity['email'],password_hash(secrets.token_urlsafe(64)),'customer'))
        uid=cur.lastrowid
    else:
        uid=link_uid
        if not conn.execute('SELECT id FROM users WHERE id=?',(uid,)).fetchone():
            raise SocialAuthError('ادخل لحسابك مرة أخرى قبل الربط.')
        if conn.execute('SELECT 1 FROM social_identities WHERE user_id=? AND provider=?',(uid,identity['provider'])).fetchone():
            raise SocialAuthError('لديك حساب مربوط بهذا المزود بالفعل.')
    conn.execute('INSERT INTO social_identities(provider,subject,user_id,auth_user_id) VALUES(?,?,?,?)',
                 (identity['provider'],identity['subject'],uid,identity['auth_user_id']))
    return conn.execute('SELECT id,role FROM users WHERE id=?',(uid,)).fetchone()
