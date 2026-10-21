"""Signed WhatsApp customer-service webhooks; no unsolicited messages or AI calls."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request

SCHEMA = """
CREATE TABLE IF NOT EXISTS wa_support_conversations (
 sender TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
 updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS wa_support_messages (
 message_id TEXT PRIMARY KEY, sender TEXT NOT NULL,
 received_at INTEGER NOT NULL, incoming_text TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL, reply_text TEXT NOT NULL DEFAULT '',
 provider_message_id TEXT UNIQUE, error_code TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wa_support_sender_time
 ON wa_support_messages(sender, received_at);
CREATE INDEX IF NOT EXISTS idx_wa_support_state_time
 ON wa_support_messages(state, received_at);
"""
REQUIRED = ('SHOPY_WA_ACCESS_TOKEN', 'SHOPY_WA_PHONE_NUMBER_ID',
            'SHOPY_WA_GRAPH_VERSION', 'SHOPY_WA_APP_SECRET', 'SHOPY_WA_VERIFY_TOKEN',
            'SHOPY_PUBLIC_URL')


def init_support_store(conn):
    conn.executescript(SCHEMA)
    if os.getenv('DATABASE_URL'):
        for table in ('wa_support_conversations', 'wa_support_messages'):
            conn.execute('ALTER TABLE ' + table + ' ENABLE ROW LEVEL SECURITY')
            conn.execute('REVOKE ALL ON TABLE ' + table + ' FROM anon, authenticated')


def configuration():
    missing = [key for key in REQUIRED if not os.getenv(key, '').strip()]
    invalid = []
    if os.getenv('SHOPY_WA_PHONE_NUMBER_ID') and not re.fullmatch(r'\d{7,25}', os.getenv('SHOPY_WA_PHONE_NUMBER_ID', '')):
        invalid.append('SHOPY_WA_PHONE_NUMBER_ID')
    if os.getenv('SHOPY_WA_GRAPH_VERSION') and not re.fullmatch(r'v\d+\.\d+', os.getenv('SHOPY_WA_GRAPH_VERSION', '')):
        invalid.append('SHOPY_WA_GRAPH_VERSION')
    if os.getenv('SHOPY_WA_VERIFY_TOKEN') and len(os.getenv('SHOPY_WA_VERIFY_TOKEN', '')) < 24:
        invalid.append('SHOPY_WA_VERIFY_TOKEN')
    if os.getenv('SHOPY_PUBLIC_URL'):
        try:
            public_base()
        except ValueError:
            invalid.append('SHOPY_PUBLIC_URL')
    return dict(missing=missing, invalid=invalid, ready=not missing and not invalid,
                enabled=os.getenv('SHOPY_WA_SUPPORT_ENABLED') == '1',
                environment_ok=not os.getenv('VERCEL') or os.getenv('VERCEL_ENV') == 'production')


def active(conn):
    row = conn.execute("SELECT value FROM store_settings WHERE key='wa_support_active'").fetchone()
    return bool(row and row['value'] == '1')


def valid_signature(raw, signature):
    secret = os.getenv('SHOPY_WA_APP_SECRET', '')
    if not secret or not re.fullmatch(r'sha256=[0-9a-f]{64}', signature or ''):
        return False
    expected = 'sha256=' + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def public_base():
    value = os.getenv('SHOPY_PUBLIC_URL', '').rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Configure the verified HTTPS store URL')
    return value


def normalized(text):
    value = re.sub(r'[\u064b-\u065f\u0670\u0640]', '', str(text).lower())
    return re.sub(r'[إأآ]', 'ا', value).replace('ة', 'ه').replace('ى', 'ي')


@dataclass(frozen=True)
class Reply:
    text: str
    handoff: bool = False


def prepare_reply(text, products, price_of, available):
    """Use only supplied public catalog data, never customer/order records."""
    base = public_base()
    n = normalized(text)
    if n.strip() in ('stop', 'ايقاف', 'وقف') or any(word in n for word in ('stop sending', 'متبعتش', 'ماتبعتش', 'متردش', 'وقف الرسايل', 'وقف الرسائل')):
        return Reply('تم إيقاف الردود التلقائية للمحادثة. لو احتجت مساعدة بشرية، وسائل التواصل هنا: ' + base + '/#contact', True)
    if any(word in n for word in ('استرجاع', 'استرداد', 'شكوي', 'شكوه', 'فلوسي',
                                 'الغاء', 'تعديل الطلب', 'تغيير الطلب', 'خدمه العملاء',
                                 'موظف', 'اكلم حد', 'بشري', 'تتبع', 'طلبي', 'طلب رقم',
                                 'عنوان صاحب', 'رقم صاحب', 'كلمه السر', 'بيانات العملاء',
                                 'تجاهل التعليمات', 'ignore instructions')):
        return Reply('استفسارك محتاج متابعة بشرية، وتم تسجيله لفريق دعم Shoppy. '
                     'متبعتش كلمة سر أو بيانات دفع هنا. تفاصيل التواصل: ' + base + '/#contact', True)
    if any(word in n for word in ('شحن', 'توصيل', 'يوصل', 'بكره', 'بكرة')):
        return Reply('تكلفة الشحن والإجمالي بيتحسبوا على الموقع حسب المنتجات والعنوان قبل تأكيد الطلب. '
                     'راجع التفاصيل هنا: ' + base + '/shipping-returns\n'
                     'ميعاد الوصول محتاج تأكيد من فريق الدعم؛ ما نقدرش نضمن تاريخ من غير تأكيد.')
    if any(word in n for word in ('استبدال', 'سياسه', 'ضمان')):
        return Reply('راجع شروط الشحن والاستبدال الحالية هنا: ' + base + '/shipping-returns\n'
                     'لو عندك حالة خاصة، اكتب «خدمة العملاء» لتحويلها لمتابعة بشرية.')
    if any(word in n for word in ('اشتري', 'اطلب ازاي', 'الدفع', 'ادفع', 'checkout')):
        return Reply('تقدر تختار المنتج وخياراته وتكمل الطلب من الموقع: ' + base + '\n'
                     'طريقة الدفع المتاحة والإجمالي النهائي بيظهروا قبل تأكيد الطلب.')
    stop_words = {'عايز', 'عاوزه', 'عايزه', 'محتاج', 'محتاجه', 'ممكن', 'عندكم',
                  'في', 'من', 'هو', 'هي', 'ده', 'دي', 'بكام', 'سعر', 'السعر',
                  'موجود', 'متاح', 'منتج', 'المنتج', 'لو', 'سمحت', 'انا', 'هل'}
    tokens = set(re.findall(r'[\w\u0600-\u06ff]{2,}', n)) - stop_words
    matches = []
    for p in products:
        title = normalized(p['title'])
        category = normalized(p['category'] or '')
        score = sum(4 if token in title else 1 if token in category else 0 for token in tokens)
        if score:
            matches.append((score, p))
    matches.sort(key=lambda item: (-item[0], item[1]['id']))
    if matches:
        lines = ['دي نتائج مرتبطة بسؤالك من كتالوج المتجر:']
        for _, product in matches[:3]:
            title = ' '.join(product['title'].split())[:140]
            price = price_of(product)
            if isinstance(price, bool) or not isinstance(price, int) or price < 0:
                continue
            stock_note = 'متاح للطلب حسب آخر تحديث على الموقع' if available(product) else 'التوفر محتاج تأكيد من صفحة المنتج أو الدعم'
            lines.extend([f'{title} — السعر المعروض {price / 100:,.2f} ج.م', stock_note,
                          base + '/products/' + quote(product['slug'], safe='-')])
        lines.append('الشحن والعروض والإجمالي النهائي بيتأكدوا عند إتمام الطلب على الموقع.')
        return Reply('\n'.join(lines)[:3500])
    if tokens and not any(word in n for word in ('اهلا', 'السلام', 'مرحبا', 'hello', 'hi')):
        return Reply('محتاج اسم المنتج أو لينكه عشان أقدر أساعدك بدقة. '
                     'تقدر تتصفح المنتجات هنا: ' + base)
    return Reply('أهلًا بحضرتك في دعم Shoppy. ابعت اسم المنتج أو لينكه، '
                 'أو اسأل عن طريقة الطلب والشحن. للمتابعة البشرية اكتب «خدمة العملاء».\n' + base)


def send_text(sender, text):
    url = ('https://graph.facebook.com/' + os.environ['SHOPY_WA_GRAPH_VERSION'] + '/' +
           os.environ['SHOPY_WA_PHONE_NUMBER_ID'] + '/messages')
    payload = dict(messaging_product='whatsapp', recipient_type='individual',
                   to=sender, type='text', text=dict(body=text, preview_url=False))
    req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={'Authorization': 'Bearer ' + os.environ['SHOPY_WA_ACCESS_TOKEN'],
                                          'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=3) as response:
        result = json.loads(response.read(32000))
    message_id = ((result.get('messages') or [{}])[0]).get('id')
    if not isinstance(message_id, str) or not message_id or len(message_id) > 256:
        raise ValueError('Missing provider message ID')
    return message_id


def process_message(db, message, reply_builder, post=None):
    """Commit a unique reservation before sending; never blindly retry uncertain sends."""
    post = post or send_text
    mid, sender = message.get('id'), message.get('from')
    if not isinstance(mid, str) or not mid or len(mid) > 256 or not isinstance(sender, str) or not re.fullmatch(r'\d{7,15}', sender):
        return 'ignored'
    now = int(time.time())
    try:
        sent_at = int(message.get('timestamp', '0'))
    except (ValueError, TypeError):
        return 'ignored'
    text_part = message.get('text')
    if message.get('type') == 'text' and not isinstance(text_part, dict):
        return 'ignored'
    body = text_part.get('body', '') if message.get('type') == 'text' else ''
    if not isinstance(body, str):
        return 'ignored'
    with db() as conn:
        # Release message contents after 30 days; retain IDs to prevent replay sends.
        conn.execute("UPDATE wa_support_messages SET sender='',incoming_text='',reply_text='' WHERE received_at<? AND sender<>''", (now - 30 * 86400,))
        conn.execute('DELETE FROM wa_support_conversations WHERE updated_at<?', (now - 30 * 86400,))
        inserted = conn.execute("INSERT OR IGNORE INTO wa_support_messages(message_id,sender,received_at,incoming_text,state) VALUES(?,?,?,?,?)",
                                (mid, sender, now, body[:2000], 'received'))
        if inserted.rowcount != 1:
            return 'duplicate'
        conn.execute('INSERT OR IGNORE INTO wa_support_conversations(sender,paused,updated_at) VALUES(?,0,?)', (sender, now))
        conn.execute('UPDATE wa_support_conversations SET updated_at=? WHERE sender=?', (now, sender))
        conv = conn.execute('SELECT paused FROM wa_support_conversations WHERE sender=?', (sender,)).fetchone()
        cfg = configuration()
        count = conn.execute('SELECT COUNT(*) AS n FROM wa_support_messages WHERE sender=? AND received_at>?', (sender, now - 60)).fetchone()['n']
        state = ('expired' if sent_at < now - 86400 + 5 or sent_at > now + 300 else
                 'paused' if conv['paused'] else 'rate_limited' if count > 10 else
                 'disabled' if not (cfg['ready'] and cfg['enabled'] and cfg['environment_ok'] and active(conn)) else '')
        if state:
            conn.execute('UPDATE wa_support_messages SET state=? WHERE message_id=?', (state, mid))
            return state
        conn.execute("UPDATE wa_support_messages SET state='processing' WHERE message_id=?", (mid,))
    try:
        reply = reply_builder(body) if body else Reply('الرسالة دي محتاجة متابعة من فريق الدعم. تم تسجيل طلب المتابعة.', True)
        if not reply.text or len(reply.text) > 4096:
            raise ValueError('Invalid response')
    except Exception:
        with db() as conn:
            conn.execute("UPDATE wa_support_messages SET state='handoff',error_code='reply_unavailable' WHERE message_id=?", (mid,))
            conn.execute('UPDATE wa_support_conversations SET paused=1,updated_at=? WHERE sender=?', (now, sender))
        return 'handoff'
    with db() as conn:
        # Recheck the kill switch immediately before any external operation.
        cfg = configuration()
        conv = conn.execute('SELECT paused FROM wa_support_conversations WHERE sender=?', (sender,)).fetchone()
        if int(time.time()) - sent_at >= 86400:
            conn.execute("UPDATE wa_support_messages SET state='expired' WHERE message_id=?", (mid,))
            return 'expired'
        if not (cfg['ready'] and cfg['enabled'] and cfg['environment_ok'] and active(conn)) or conv['paused']:
            conn.execute("UPDATE wa_support_messages SET state='paused' WHERE message_id=?", (mid,))
            return 'paused'
        conn.execute("UPDATE wa_support_messages SET state='sending',reply_text=? WHERE message_id=?", (reply.text, mid))
        if reply.handoff:
            conn.execute('UPDATE wa_support_conversations SET paused=1,updated_at=? WHERE sender=?', (now, sender))
    try:
        provider_id = post(sender, reply.text)
        if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 256:
            raise ValueError('Invalid provider response')
        with db() as conn:
            conn.execute('UPDATE wa_support_messages SET state=?,provider_message_id=? WHERE message_id=?',
                         ('handoff' if reply.handoff else 'accepted', provider_id, mid))
        return 'handoff' if reply.handoff else 'accepted'
    except Exception:
        with db() as conn:
            conn.execute("UPDATE wa_support_messages SET state='unknown',error_code='provider_outcome_unknown' WHERE message_id=?", (mid,))
            conn.execute('UPDATE wa_support_conversations SET paused=1,updated_at=? WHERE sender=?', (now, sender))
        return 'unknown'


def register_support(app, db, admin_only, reply_builder):
    bp = Blueprint('wa_support', __name__)

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return response

    @bp.route('/webhooks/whatsapp', methods=['GET', 'POST'])
    def webhook():
        if request.method == 'GET':
            token = os.getenv('SHOPY_WA_VERIFY_TOKEN', '')
            candidate = request.args.get('hub.verify_token', '')
            if len(token) < 24:
                return jsonify(error='not_configured'), 503
            if request.args.get('hub.mode') != 'subscribe' or not hmac.compare_digest(token.encode(), candidate.encode()):
                abort(403)
            challenge = request.args.get('hub.challenge', '')
            if not re.fullmatch(r'[0-9]{1,100}', challenge):
                abort(400)
            return app.response_class(challenge, mimetype='text/plain')
        if not os.getenv('SHOPY_WA_APP_SECRET') or not os.getenv('SHOPY_WA_PHONE_NUMBER_ID'):
            return jsonify(error='not_configured'), 503
        if request.content_length is None or request.content_length > 131072:
            return jsonify(error='payload_size'), 413
        raw = request.get_data()
        if not valid_signature(raw, request.headers.get('X-Hub-Signature-256', '')):
            abort(401)
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get('object') != 'whatsapp_business_account':
            abort(400)
        entries = payload.get('entry')
        if not isinstance(entries, list) or len(entries) > 20:
            abort(400)
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('changes'), list):
                abort(400)
            for change in entry['changes']:
                if not isinstance(change, dict) or not isinstance(change.get('value'), dict):
                    abort(400)
                value = change['value']
                metadata = value.get('metadata')
                if not isinstance(metadata, dict):
                    abort(400)
                if change.get('field') != 'messages' or metadata.get('phone_number_id') != os.getenv('SHOPY_WA_PHONE_NUMBER_ID'):
                    continue
                messages, statuses = value.get('messages', []), value.get('statuses', [])
                if not isinstance(messages, list) or not isinstance(statuses, list) or len(messages) > 20:
                    abort(400)
                for message in messages:
                    if isinstance(message, dict):
                        process_message(db, message, reply_builder)
                for status in statuses:
                    if not isinstance(status, dict) or status.get('status') not in ('delivered', 'read', 'failed') or not isinstance(status.get('id'), str):
                        continue
                    with db() as conn:
                        conn.execute("UPDATE wa_support_messages SET state=? WHERE provider_message_id=? AND state IN ('accepted','delivered')",
                                     (status['status'], status['id'][:256]))
        return jsonify(ok=True)

    @bp.get('/admin/whatsapp-support')
    @admin_only
    def dashboard():
        cfg = configuration()
        with db() as conn:
            enabled = active(conn)
            rows = conn.execute('SELECT m.*,c.paused FROM wa_support_messages m JOIN wa_support_conversations c ON c.sender=m.sender ORDER BY received_at DESC LIMIT 50').fetchall()
        return render_template('whatsapp_support.html', wa_config=cfg, wa_active=enabled, wa_messages=rows)

    @bp.post('/admin/whatsapp-support/control')
    @admin_only
    def control():
        action = request.form.get('action', '')
        if action not in ('start', 'stop', 'resume', 'pause'):
            abort(400)
        cfg = configuration()
        if action == 'start' and not (cfg['ready'] and cfg['enabled'] and cfg['environment_ok']):
            flash('الربط محتاج استكمال الإعدادات قبل تشغيل الردود.')
            return redirect('/admin/whatsapp-support')
        with db() as conn:
            if action in ('start', 'stop'):
                conn.execute("INSERT INTO store_settings(key,value) VALUES('wa_support_active',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if action == 'start' else '0',))
            else:
                sender = request.form.get('sender', '')
                if not re.fullmatch(r'\d{7,15}', sender):
                    abort(400)
                conn.execute('UPDATE wa_support_conversations SET paused=?,updated_at=? WHERE sender=?', (0 if action == 'resume' else 1, int(time.time()), sender))
        flash('تم حفظ إعداد الردود. الرسائل السابقة لا يعاد إرسالها تلقائيًا.')
        return redirect('/admin/whatsapp-support')

    app.register_blueprint(bp)
