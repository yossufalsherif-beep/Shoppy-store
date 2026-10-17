"""HTTP tests (run locally after installing Flask). No live API calls."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as store
from werkzeug.security import generate_password_hash
from safka_cleanup import CONFIRM_TEXT

class SafkaAdminFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root_patcher=patch.object(store,'ROOT',Path(self.tmp.name))
        self.root_patcher.start()
        self.patcher=patch.object(store,'DB',str(Path(self.tmp.name)/'test.sqlite3'))
        self.patcher.start()
        store.init()
        store.app.config.update(TESTING=True,SECRET_KEY='testing-secret-only')
        self.client=store.app.test_client()
        with store.db() as c:
            c.execute("INSERT INTO users(email,password,role) VALUES(?,?,?)",('admin@test.local',generate_password_hash('admin-passphrase-123'),'admin'))
    def tearDown(self):
        self.patcher.stop()
        self.root_patcher.stop()
        self.tmp.cleanup()
    def token(self):
        with self.client.session_transaction() as sess:
            sess['csrf']='test-token'
        return 'test-token'
    def login(self):
        return self.client.post('/login',data={'csrf':self.token(),'email':'admin@test.local','password':'admin-passphrase-123'})
    def test_starts_without_demo_product_or_shipping(self):
        with store.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM products').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM shipping').fetchone()[0],0)
        self.assertEqual(self.client.get('/').status_code,200)
    def test_cleanup_requires_admin_and_real_sync(self):
        payload={'csrf':self.token(),'confirmation':CONFIRM_TEXT}
        self.assertEqual(self.client.post('/admin/safka/replace-manual-catalog',data=payload).status_code,302)
        self.login()
        with store.db() as c:
            c.execute("INSERT INTO products(title,slug,description,price,source) VALUES('Manual','manual','',10000,'manual')")
            c.execute("INSERT INTO shipping(governorate,fee) VALUES('القاهرة',6000)")
        payload['csrf']=self.token()  # login rotates CSRF session
        attempt=self.client.post('/admin/safka/replace-manual-catalog',data=payload,follow_redirects=True)
        self.assertEqual(attempt.status_code,200)
        with store.db() as c:self.assertEqual(c.execute("SELECT COUNT(*) FROM products WHERE source='manual'").fetchone()[0],1)
    def test_cleanup_verified_live_and_prevents_manual_reentry(self):
        self.login()
        with store.db() as c:
            c.execute("INSERT INTO products(title,slug,description,price,source) VALUES('Manual','manual','',10000,'manual')")
            c.execute("INSERT INTO products(title,slug,description,price,source,supplier_product_id,supplier_stock,supplier_last_sync) VALUES('Safka','safka-1','',14000,'safka','S1',5,'2026-10-16T00:00:00+00:00')")
            c.execute("INSERT INTO shipping(governorate,fee) VALUES('القاهرة',6000)")
            c.execute("INSERT INTO safka_shipping_rates(governorate,city,area,supplier_fee,fee,updated_at) VALUES('القاهرة','','',5000,6000,'2026-10-16T00:00:00+00:00')")
            c.execute("INSERT INTO safka_sync_runs(kind,updated,created_at,source_label) VALUES('products',1,'now','api')")
            c.execute("INSERT INTO safka_sync_runs(kind,updated,created_at,source_label) VALUES('shipping',1,'now','api')")
        resp=self.client.post('/admin/safka/replace-manual-catalog',data={'csrf':self.token(),'confirmation':CONFIRM_TEXT},follow_redirects=True)
        self.assertEqual(resp.status_code,200)
        with store.db() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM products WHERE source='manual'").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT value FROM store_settings WHERE key='catalog_mode'").fetchone()[0],'safka_only')
        blocked=self.client.post('/admin/product',data={'csrf':self.token(),'title':'Should block','slug':'should-block','price':'20'})
        self.assertEqual(blocked.status_code,403)

if __name__=='__main__':unittest.main()
