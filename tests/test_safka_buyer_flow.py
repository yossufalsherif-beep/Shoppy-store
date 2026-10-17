"""End-to-end local buyer flow with supplier dispatch MOCKED (no network writes)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import app as store

class SafkaBuyerFlowTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  self.db_patch=patch.object(store,'DB',str(Path(self.tmp.name)/'safka-buyer-test.sqlite3'))
  self.db_patch.start()
  store.init()
  store.app.config.update(TESTING=True,SECRET_KEY='buyer-flow-test-secret')
  self.client=store.app.test_client()
  with store.db() as conn:
   conn.execute("""INSERT INTO products(title,slug,description,price,image,active,options_json,category,
            supplier_cost,source,supplier_product_id,supplier_stock,supplier_available,supplier_listed,supplier_last_sync)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
       ('منتج صفقة','safka-test','وصف',15000,'',1,'{}','إلكترونيات',
        10000,'safka','675a00000000000000000001',None,1,1,'2026-10-17T16:00:00+00:00'))
   conn.execute("""INSERT INTO safka_shipping_rates(
        governorate,city,area,supplier_fee,fee,updated_at,pricing_document_id,city_id)
        VALUES(?,?,?,?,?,?,?,?)""",
        ('الجيزة','6 أكتوبر','',8500,9500,'2026-10-17T16:00:00+00:00','675a00000000000000000003','336'))
 def tearDown(self):
  self.db_patch.stop()
  self.tmp.cleanup()
 def token(self):
  with self.client.session_transaction() as sess:sess['csrf']='owner-test-csrf'
  return 'owner-test-csrf'
 def test_authenticated_checkout_saves_then_dispatches_single_safka_order(self):
  self.client.post('/register',data={'csrf':self.token(),'email':'test-buyer@shopy.test','password':'safe-long-password'})
  with store.db() as c:pid=c.execute("SELECT id FROM products WHERE source='safka'").fetchone()['id']
  with patch.object(store,'supplier_checkout_live',return_value=True),\
       patch('safka_auto_orders.verify_checkout_products',return_value={}),\
       patch('safka_auto_orders.submit_saved_order_once') as send:
   add=self.client.post('/cart/add/'+str(pid),data={'csrf':self.token()})
   self.assertEqual(add.status_code,302)
   payload={
      'csrf':self.token(),'name':'Test Buyer','phone':'01012345678',
      'governorate':'الجيزة','city':'6 أكتوبر','area':'الحي السابع',
      'address':'شارع وهمي رقم 8 عمارة 4'
   }
   payload['quote_token']=self.client.get('/api/shipping-quote',query_string={
      'governorate':payload['governorate'],'city':payload['city'],'area':payload['area']}).json['quote_token']
   done=self.client.post('/checkout',data=payload)
   self.assertEqual(done.status_code,302)
   self.assertIn('/order/success/',done.headers['Location'])
   send.assert_called_once()
   account=self.client.get('/account')
   self.assertEqual(account.status_code,200)
   self.assertIn('منتج صفقة'.encode(),account.data)
   with store.db() as c:
    order=c.execute('SELECT * FROM orders LIMIT 1').fetchone()
    state=c.execute('SELECT * FROM safka_order_outbox WHERE order_id=?',(order['id'],)).fetchone()
    self.assertIsNotNone(state)
    self.assertEqual(order['total'],24500)
    self.assertEqual(order['shipping_fee'],9500)

if __name__=='__main__':unittest.main()
