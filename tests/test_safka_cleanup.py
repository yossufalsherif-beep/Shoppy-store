"""Pure SQLite tests: no internet, no Flask dependency, no customer messages."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from safka_cleanup import summary, cleanup, CleanupNotReady, CONFIRM_TEXT


class CatalogCleanupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db_path = Path(self.folder.name) / 'shopy.db'
        self.c = sqlite3.connect(str(self.db_path))
        self.c.row_factory = sqlite3.Row
        self.c.execute('PRAGMA foreign_keys=ON')
        self.c.executescript('''
        CREATE TABLE users(id INTEGER PRIMARY KEY,email TEXT,role TEXT);
        CREATE TABLE products(id INTEGER PRIMARY KEY,title TEXT,source TEXT NOT NULL,supplier_product_id TEXT,active INTEGER);
        CREATE TABLE product_media(id INTEGER PRIMARY KEY,product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,path TEXT);
        CREATE TABLE shipping(id INTEGER PRIMARY KEY,governorate TEXT,fee INTEGER);
        CREATE TABLE shipping_zones(id INTEGER PRIMARY KEY,governorate TEXT,city TEXT,fee INTEGER);
        CREATE TABLE safka_shipping_rates(id INTEGER PRIMARY KEY,fee INTEGER);
        CREATE TABLE safka_sync_runs(id INTEGER PRIMARY KEY,kind TEXT,updated INTEGER,created_at TEXT,source_label TEXT);
        CREATE TABLE store_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),reference TEXT,total INTEGER,shipping_fee INTEGER);
        CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER REFERENCES orders(id),product_id INTEGER REFERENCES products(id),title TEXT,unit_price INTEGER,quantity INTEGER);
        INSERT INTO users VALUES(1,'private@test.local','admin');
        INSERT INTO products VALUES(1,'Old manual','manual',NULL,1);
        INSERT INTO products VALUES(2,'Safka item','safka','S1',1);
        INSERT INTO product_media VALUES(1,1,'/static/uploads/old.png');
        INSERT INTO shipping VALUES(1,'القاهرة',6000);
        INSERT INTO shipping_zones VALUES(1,'القاهرة','مدينة نصر',7000);
        INSERT INTO safka_shipping_rates VALUES(1,5500);
        INSERT INTO orders VALUES(1,1,'OLD-ORDER',16000,6000);
        INSERT INTO order_items VALUES(1,1,1,'Old manual',10000,1);
        ''')
        self.c.commit()
    def tearDown(self):
        self.c.close()
        self.folder.cleanup()
    def mark_live(self):
        self.c.executemany("INSERT INTO safka_sync_runs(kind,updated,created_at,source_label) VALUES(?,2,'2026-10-16T00:00Z','api')", [('products',),('shipping',)])
        self.c.commit()
    def test_cannot_cleanup_before_both_real_syncs(self):
        with self.assertRaises(CleanupNotReady):
            cleanup(self.c,self.db_path,self.db_path.parent/'backups',CONFIRM_TEXT)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM shipping').fetchone()[0],1)
        self.assertEqual(list((self.db_path.parent/'backups').glob('*.sqlite3')) if (self.db_path.parent/'backups').exists() else [],[])
    def test_local_json_not_evidence_of_real_sync(self):
        self.c.execute("INSERT INTO safka_sync_runs(kind,updated,created_at,source_label) VALUES('products',3,'today','local-json')")
        self.c.execute("INSERT INTO safka_sync_runs(kind,updated,created_at,source_label) VALUES('shipping',3,'today','local-json')")
        self.c.commit()
        self.assertFalse(summary(self.c)['ready'])
    def test_local_fixture_after_live_sync_blocks_cleanup(self):
        self.mark_live()
        self.assertTrue(summary(self.c)['ready'])
        self.c.execute("INSERT INTO safka_sync_runs(kind,updated,created_at,source_label) VALUES('products',2,'later','local-json')")
        self.c.commit()
        self.assertFalse(summary(self.c)['ready'])

    def test_typo_does_not_delete(self):
        self.mark_live()
        with self.assertRaises(CleanupNotReady):
            cleanup(self.c,self.db_path,self.db_path.parent/'backups','oops')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM products').fetchone()[0],2)
    def test_snapshot_preservation_and_restorable_backup(self):
        self.mark_live()
        before = self.c.execute('SELECT * FROM order_items WHERE id=1').fetchone()
        result=cleanup(self.c,self.db_path,self.db_path.parent/'backups',CONFIRM_TEXT)
        self.assertEqual((result['removed_products'],result['removed_shipping'],result['removed_zones']),(1,1,1))
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM products WHERE source='manual'").fetchone()[0],0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM products WHERE source='safka'").fetchone()[0],1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM shipping').fetchone()[0],0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM shipping_zones').fetchone()[0],0)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],1)
        old = self.c.execute('SELECT * FROM order_items WHERE id=1').fetchone()
        self.assertIsNone(old['product_id'])
        self.assertEqual((old['title'],old['unit_price'],old['quantity']),(before['title'],before['unit_price'],before['quantity']))
        self.assertEqual(self.c.execute("SELECT value FROM store_settings WHERE key='catalog_mode'").fetchone()[0],'safka_only')
        with sqlite3.connect(result['backup_path']) as backup:
            self.assertEqual(backup.execute('SELECT COUNT(*) FROM products').fetchone()[0],2)
            self.assertEqual(backup.execute('SELECT product_id FROM order_items WHERE id=1').fetchone()[0],1)
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0],'ok')
    def test_empty_supplier_rates_is_not_ready(self):
        self.mark_live()
        self.c.execute('DELETE FROM safka_shipping_rates')
        self.c.commit()
        self.assertFalse(summary(self.c)['ready'])

if __name__=='__main__': unittest.main()
