"""Promo codes: owner access, percentages, expiry, redemption and Safka totals.

Uses temporary local SQLite databases and mocked supplier transport only.
"""
import time
import unittest
from unittest.mock import patch
from promo_codes import (create_code,get_active_code,calculate_discount,
                         consume_code,PromoError)
from tests.test_store import StoreTests
from tests.test_safka_checkout import AutomaticCheckoutTests
from safka_live_orders import OrderSubmissionOutcome
from safka_dry_run import prepare_dry_run
import app as store


class PromoAdminTests(StoreTests):
    def test_public_customer_promo_page_and_protected_admin(self):
        self.assertEqual(self.client.get('/promo-code').status_code,200)
        self.assertEqual(self.client.get('/admin/promo-codes').status_code,302)
        self.client.post('/register',data={'csrf':self.token(),
                        'email':'person@example.test','password':'a-strong-password-2026'})
        self.assertEqual(self.client.get('/admin/promo-codes').status_code,403)

    def test_admin_create_limit_edit_disable_and_expiry(self):
        self.admin_login()
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        now=datetime.now(ZoneInfo('Africa/Cairo'))
        start=(now-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        expiry=(now+timedelta(days=5)).strftime('%Y-%m-%dT%H:%M')
        resp=self.client.post('/admin/promo-codes/create',data={
            'csrf':self.token(),'code':'welcome10','percent':'10','max_uses':'2',
            'starts_at':start,'expires_at':expiry})
        self.assertEqual(resp.status_code,302)
        with store.db() as conn:
            saved=conn.execute("SELECT * FROM promo_codes WHERE code='WELCOME10'").fetchone()
        self.assertEqual(saved['percent'],10)
        self.assertEqual(saved['max_uses'],2)
        self.assertEqual(self.client.get('/admin/promo-codes').status_code,200)
        edited=self.client.post('/admin/promo-codes/WELCOME10/edit',data={
            'csrf':self.token(),'percent':'15','unlimited':'1',
            'starts_at':start,'expires_at':expiry})
        self.assertEqual(edited.status_code,302)
        with store.db() as conn:
            row=conn.execute("SELECT * FROM promo_codes WHERE code='WELCOME10'").fetchone()
            self.assertEqual((row['percent'],row['max_uses']),(15,None))
        self.client.post('/admin/promo-codes/WELCOME10/toggle',data={'csrf':self.token()})
        with store.db() as conn:
            with self.assertRaises(PromoError):get_active_code(conn,'WELCOME10')

    def test_expires_and_usage_claim_is_atomic_even_at_zero_stock(self):
        now=int(time.time())
        with store.db() as conn:
            create_code(conn,'LIMIT1',15,1,now-1,now+3600,now=now)
            consume_code(conn,'LIMIT1',now=now)
            with self.assertRaises(PromoError):consume_code(conn,'LIMIT1',now=now)
            self.assertEqual(conn.execute("SELECT used_count FROM promo_codes WHERE code='LIMIT1'").fetchone()[0],1)
            with self.assertRaises(PromoError):get_active_code(conn,'LIMIT1',now=now)
            create_code(conn,'EXPIRED',5,None,now-200,now+1,now=now)
            with self.assertRaises(PromoError):get_active_code(conn,'EXPIRED',now=now+1)
            create_code(conn,'STARTS',5,None,now+3600,now+7200,now=now)
            with self.assertRaises(PromoError):get_active_code(conn,'STARTS',now=now)

    def test_invalid_codes_limits_and_duplicate_refused(self):
        now=int(time.time())
        with store.db() as conn:
            for text in ('xx','HELLO WORLD','../../','🔥🔥🔥🔥'):
                with self.subTest(text=text),self.assertRaises(PromoError):
                    create_code(conn,text,10,None,now,now+500,now=now)
            with self.assertRaises(PromoError):
                create_code(conn,'HELLO',0,None,now,now+500,now=now)
            with self.assertRaises(PromoError):
                create_code(conn,'HELLO',10,0,now,now+500,now=now)
            with self.assertRaises(PromoError):
                create_code(conn,'HELLO',10,1,now,now-1,now=now)
            create_code(conn,'HELLO',10,None,now,now+500,now=now)
            with self.assertRaises(PromoError):
                create_code(conn,'hello',10,None,now,now+500,now=now)


class PromoCheckoutTests(AutomaticCheckoutTests):
    def setUp(self):
        super().setUp()
        now=int(time.time())
        with store.db() as conn:
            create_code(conn,'SAVE10',10,1,now-100,now+3600,now=now)

    def apply_coupon(self):
        self.client.post('/promo-code/apply',data={'csrf':self.token(),
                         'promo_code':'save10','return_to':'/checkout'})
        response=self.client.get('/api/shipping-quote',query_string={
            k:self.delivery[k] for k in ('governorate','city','area')})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.delivery['quote_token']=response.json['quote_token']
        return response.json

    def test_quote_paid_total_supplier_snapshot_and_single_use(self):
        data=self.apply_coupon()
        self.assertEqual(data['promo_code'],'SAVE10')
        self.assertGreater(data['promo_discount_piastres'],0)
        self.assertEqual(data['promo_discount_piastres'],
                         data['merchandise_total_piastres']//10)
        with patch('safka_auto_orders.submit_payload',
                   return_value=OrderSubmissionOutcome('accepted','offline-id')) as sender:
            response=self.client.post('/checkout',data=self.delivery)
        self.assertIn('/order/success/',response.location)
        sender.assert_called_once()
        with store.db() as conn:
            o=conn.execute('SELECT * FROM orders').fetchone()
            coupon=conn.execute("SELECT * FROM promo_codes WHERE code='SAVE10'").fetchone()
            self.assertEqual(o['promo_code'],'SAVE10')
            self.assertEqual(o['promo_percent'],10)
            self.assertEqual(o['promo_discount'],data['promo_discount_piastres'])
            self.assertEqual(o['total'],
                             data['merchandise_total_piastres']-
                             o['promo_discount']+data['shipping_piastres'])
            self.assertEqual(coupon['used_count'],1)
            preview=prepare_dry_run(conn,o['id'])
            self.assertEqual(preview['total_to_collect_piastres'],o['total'])
            self.assertEqual(preview['promo_discount_piastres'],o['promo_discount'])
            conn.execute('UPDATE orders SET promo_discount=promo_discount+1')
            with self.assertRaises(Exception):prepare_dry_run(conn,o['id'])

    def test_tampered_quote_and_exhaustion_never_send(self):
        self.apply_coupon()
        with self.client.session_transaction() as sess:sess['promo_code']='EXPIRED'
        with patch('safka_auto_orders.submit_payload') as sender:
            resp=self.client.post('/checkout',data=self.delivery)
        self.assertEqual(resp.status_code,200)
        sender.assert_not_called()
        with store.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT used_count FROM promo_codes WHERE code='SAVE10'").fetchone()[0],0)


class PromoMarginTests(StoreTests):
    def test_cannot_discount_below_supplier_and_shipping_costs(self):
        now=int(time.time())
        with store.db() as conn:
            create_code(conn,'TOOBIG',50,None,now-1,now+600,now=now)
            create_code(conn,'SMALL',1,None,now-1,now+600,now=now)
            p={'supplier_cost':15000}
            offer={'merchandise_total':25000,'fee':5000,'floor':100}
            with self.assertRaises(PromoError):
                calculate_discount(conn,'TOOBIG',offer,[(p,1)],(9500,8500),now=now)
            amount,percent=calculate_discount(conn,'SMALL',offer,[(p,1)],(9500,8500),now=now)
            self.assertEqual((amount,percent),(250,1))


if __name__=='__main__':
    unittest.main()
