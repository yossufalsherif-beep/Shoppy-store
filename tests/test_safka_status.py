"""Only authenticated, known and newer provider events can change supplier status."""
import sqlite3
import unittest
from safka_status import apply_order_status_event

class StatusEvents(unittest.TestCase):
 def setUp(self):
  self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
  self.conn.executescript("CREATE TABLE safka_order_outbox(order_id INTEGER PRIMARY KEY,supplier_order_id TEXT,supplier_status TEXT,supplier_status_updated_at TEXT,updated_at TEXT);INSERT INTO safka_order_outbox(order_id,supplier_order_id) VALUES(1,'675a00000000000000000001');")
 def tearDown(self):self.conn.close()
 def payload(self,status='shipped',stamp='2026-10-01T12:00:00Z',ident='675a00000000000000000001'):
  return {'event':'order.status.updated','order':{'_id':ident,'status':status,'updated_at':stamp,'client_name':'Ignored','total':0}}
 def test_duplicate_and_older_updates_do_not_replace_newer_status(self):
  self.assertTrue(apply_order_status_event(self.conn,self.payload()))
  self.assertFalse(apply_order_status_event(self.conn,self.payload()))
  self.assertFalse(apply_order_status_event(self.conn,self.payload('preparing','2026-10-01T11:00:00Z')))
  self.assertEqual(self.conn.execute('SELECT supplier_status FROM safka_order_outbox').fetchone()[0],'shipped')
 def test_unknown_id_unknown_status_and_missing_timezone_are_ignored(self):
  for payload in [self.payload(ident='675a00000000000000000002'),self.payload(status='invented'),self.payload(status={}),self.payload(stamp='2026-10-01T12:00:00')]:
   self.assertFalse(apply_order_status_event(self.conn,payload))
  self.assertIsNone(self.conn.execute('SELECT supplier_status FROM safka_order_outbox').fetchone()[0])
 def test_same_instant_with_different_timezone_is_not_a_new_update(self):
  self.assertTrue(apply_order_status_event(self.conn,self.payload()))
  self.assertFalse(apply_order_status_event(self.conn,self.payload('preparing','2026-10-01T15:00:00+03:00')))
