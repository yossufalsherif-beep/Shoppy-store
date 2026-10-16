import re
import unittest
from pathlib import Path
from pg_backend import _translate, PGRow, SERIAL_TABLES
from safka_bridge import sale_price_piastres, customer_shipping_piastres

ROOT=Path(__file__).resolve().parents[1]

class PGStagingOfflineTests(unittest.TestCase):
 def test_integer_key_and_named_row_access(self):
  r=PGRow({'count':3,'name':'x'})
  self.assertEqual(r[0],3)
  self.assertEqual(r['name'],'x')
  self.assertEqual(tuple(r),(3,'x'))
  self.assertEqual(dict(r),{'count':3,'name':'x'})
 def test_sqlite_insert_ignore_is_safe_pg_conflict(self):
  sql,_=_translate('INSERT OR IGNORE INTO safka_order_outbox(order_id,created_at,updated_at) VALUES(?,?,?)')
  self.assertIn('ON CONFLICT DO NOTHING',sql)
  self.assertEqual(sql.count('%s'),3)
 def test_replace_autoincrement(self):
  sql,_=_translate('CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT)')
  self.assertIn('id BIGSERIAL PRIMARY KEY',sql)
 def test_parameterized_like(self):
  sql,_=_translate("SELECT * FROM x WHERE path LIKE 'https://%' AND id=?")
  self.assertIn('https://%%',sql)
  self.assertIn('id=%s',sql)
 def test_pg_introspection_only_public_schema(self):
  sql,args=_translate('PRAGMA table_info(products)')
  self.assertIn("table_schema='public'",sql)
  self.assertIn("table_name='products'",sql)
  self.assertEqual(args,())
 def test_supabase_tables_rls_without_permissive_policy(self):
  schema=(ROOT/'supabase_schema.sql').read_text()
  tables=set(re.findall(r'CREATE TABLE IF NOT EXISTS\s+(\w+)',schema))
  protected=set(re.findall(r'ALTER TABLE\s+(\w+)\s+ENABLE ROW LEVEL SECURITY',schema))
  self.assertEqual(tables,protected)
  self.assertFalse(re.search(r'CREATE POLICY',schema,re.I))
  self.assertTrue(SERIAL_TABLES.issubset(tables))
 def test_safka_rules_ceil_and_shipping_addition(self):
  self.assertEqual(sale_price_piastres(10000),14000)
  self.assertEqual(sale_price_piastres(5100),7000)
  self.assertEqual(customer_shipping_piastres(4000),5000)
 def test_staging_blocks_public_checkout_and_customer_signup(self):
  source=(ROOT/'app.py').read_text()
  self.assertIn("request.path.startswith('/register')",source)
  self.assertIn("request.path.startswith('/checkout')",source)
  self.assertIn("os.getenv('SHOPY_STAGING_OWNER_CHECKOUT') == '1'",source)
  self.assertIn("owner['role'] != 'admin'",source)
  self.assertIn('STAGING_MODE',source)
  self.assertIn('SHOPY_SAFKA_ORDER_PUSH_ACTIVE', (ROOT/'.env.example').read_text())

if __name__=='__main__':unittest.main()
