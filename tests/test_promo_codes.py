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

    def test_admin_creates_fixed_shipping_code_and_edits_to_free_shipping(self):
        self.admin_login()
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        now=datetime.now(ZoneInfo('Africa/Cairo'))
        start=(now-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        expiry=(now+timedelta(days=3)).strftime('%Y-%m-%dT%H:%M')
        response=self.client.post('/admin/promo-codes/create',data={
            'csrf':self.token(),'code':'SHIP30','kind':'shipping_amount',
            'amount_egp':'30','percent':'10','min_items':'2','max_uses':'5',
            'starts_at':start,'expires_at':expiry})
        self.assertEqual(response.status_code,302)
        with store.db() as conn:
            code=conn.execute("SELECT * FROM promo_codes WHERE code='SHIP30'").fetchone()
            self.assertEqual((code['kind'],code['amount_piastres'],code['min_items'],code['max_uses']),
                             ('shipping_amount',3000,2,5))
            self.assertEqual(code['percent'],0)
        updated=self.client.post('/admin/promo-codes/SHIP30/edit',data={
            'csrf':self.token(),'kind':'free_shipping','min_items':'3',
            'unlimited':'1','starts_at':start,'expires_at':expiry})
        self.assertEqual(updated.status_code,302)
        with store.db() as conn:
            code=conn.execute("SELECT * FROM promo_codes WHERE code='SHIP30'").fetchone()
            self.assertEqual((code['kind'],code['amount_piastres'],code['min_items'],code['max_uses']),
                             ('free_shipping',0,3,None))

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



class PromoKindsTests(StoreTests):
    def setUp(self):
        super().setUp()
        self.now=int(time.time())
        self.offer={'merchandise_total':25000,'fee':5000,'floor':100}
        self.product={'supplier_cost':2000}
        self.rate=(9500,8500)

    def build(self,name,kind,amount=0,minimum=1,cap=None):
        from promo_codes import quote_promo
        with store.db() as conn:
            create_code(conn,name,0,None,self.now-1,self.now+300,
                        now=self.now,kind=kind,amount_piastres=amount,
                        min_items=minimum,max_discounted_items=cap)
            return quote_promo(conn,name,self.offer,[(self.product,3)],self.rate,now=self.now)

    def test_fixed_total_50_egp(self):
        promo=self.build('FIXED50','fixed_order',5000)
        self.assertEqual(promo['product_discount'],5000)
        self.assertEqual(promo['shipping_discount'],0)
        self.assertEqual(promo['value'],5000)

    def test_per_item_discount_only_first_two_of_three_items(self):
        promo=self.build('PIECES20','fixed_item',2000,minimum=3,cap=2)
        self.assertEqual(promo['product_discount'],4000)
        self.assertEqual(promo['min_items'],3)
        self.assertEqual(promo['max_items'],2)

    def test_minimum_quantity_is_required(self):
        with store.db() as conn:
            create_code(conn,'MINTHREE',0,None,self.now-1,self.now+300,
                        kind='fixed_order',amount_piastres=500, min_items=3,now=self.now)
            from promo_codes import quote_promo
            with self.assertRaises(PromoError):
                quote_promo(conn,'MINTHREE',self.offer,[(self.product,2)],self.rate,now=self.now)
            self.assertEqual(quote_promo(conn,'MINTHREE',self.offer,[(self.product,3)],self.rate,now=self.now)['product_discount'],500)

    def test_shipping_fixed_discount_capped_to_actual_shipping(self):
        promo=self.build('SHIP30','shipping_amount',3000)
        self.assertEqual(promo['shipping_discount'],3000)
        self.assertEqual(promo['product_discount'],0)
        clipped=self.build('SHIP90','shipping_amount',9000)
        self.assertEqual(clipped['shipping_discount'],5000)

    def test_free_shipping_and_not_on_already_free_delivery(self):
        from promo_codes import quote_promo
        promo=self.build('SHIPFREE','free_shipping')
        self.assertEqual(promo['shipping_discount'],5000)
        with store.db() as conn:
            with self.assertRaises(PromoError):
                quote_promo(conn,'SHIPFREE',dict(self.offer,fee=0),[(self.product,3)],self.rate,now=self.now)

    def test_fixed_amount_more_than_products_is_rejected(self):
        from promo_codes import quote_promo
        with store.db() as conn:
            create_code(conn,'EXCESS',0,None,self.now-1,self.now+300,
                        kind='fixed_order',amount_piastres=30000,now=self.now)
            with self.assertRaises(PromoError):
                quote_promo(conn,'EXCESS',self.offer,[(self.product,3)],self.rate,now=self.now)

    def test_margin_guard_blocks_shipping_discounts_that_lose_money(self):
        from promo_codes import quote_promo
        with store.db() as conn:
            create_code(conn,'LOSSSHIP',0,None,self.now-1,self.now+300,
                        kind='shipping_amount',amount_piastres=5000,now=self.now)
            with self.assertRaises(PromoError):
                quote_promo(conn,'LOSSSHIP',self.offer,[(dict(self.product,supplier_cost=19000),1)],self.rate,now=self.now)

    def test_invalid_config_requires_relevant_kind_values(self):
        with store.db() as conn:
            with self.assertRaises(PromoError):
                create_code(conn,'BADSHIP',0,None,self.now-1,self.now+300,
                            kind='shipping_amount',amount_piastres=0,now=self.now)
            with self.assertRaises(PromoError):
                create_code(conn,'BADCAP',0,None,self.now-1,self.now+300,
                            kind='fixed_order',amount_piastres=500,max_discounted_items=2,now=self.now)


class PromoSupplierFlowTests(AutomaticCheckoutTests):
    def setUp(self):
        super().setUp()
        now=int(time.time())
        with store.db() as conn:
            create_code(conn,'SHIP30',0,3,now-100,now+3600,
                        kind='shipping_amount',amount_piastres=3000,now=now)
            create_code(conn,'ORDER20',0,3,now-100,now+3600,
                        kind='fixed_order',amount_piastres=2000,now=now)

    def reprice(self,name):
        self.client.post('/promo-code/apply',data={'csrf':self.token(),
                         'promo_code':name,'return_to':'/checkout'})
        response=self.client.get('/api/shipping-quote',query_string={
            k:self.delivery[k] for k in ('governorate','city','area')})
        self.assertEqual(response.status_code,200,response.get_data(as_text=True))
        self.delivery['quote_token']=response.json['quote_token']
        return response.json

    def test_shipping_coupon_reconciles_provider_total_and_saved_shipping(self):
        data=self.reprice('SHIP30')
        self.assertEqual(data['promo_shipping_discount_piastres'],3000)
        self.assertEqual(data['promo_discount_piastres'],0)
        self.assertEqual(data['shipping_piastres'],6500)
        with patch('safka_auto_orders.submit_payload',
                   return_value=OrderSubmissionOutcome('accepted','offline-id')) as send:
            response=self.client.post('/checkout',data=self.delivery)
        self.assertIn('/order/success/',response.location)
        send.assert_called_once()
        with store.db() as conn:
            order=conn.execute('SELECT * FROM orders').fetchone()
            self.assertEqual(order['promo_kind'],'shipping_amount')
            self.assertEqual(order['promo_shipping_discount'],3000)
            self.assertEqual(order['shipping_fee'],6500)
            self.assertEqual(order['promo_discount'],0)
            self.assertEqual(order['total'],data['merchandise_total_piastres']+6500)
            self.assertEqual(prepare_dry_run(conn,order['id'])['total_to_collect_piastres'],order['total'])
            conn.execute('UPDATE orders SET promo_shipping_discount=promo_shipping_discount+1')
            with self.assertRaises(Exception):prepare_dry_run(conn,order['id'])

    def test_fixed_order_saves_product_discount_for_supplier_commission(self):
        data=self.reprice('ORDER20')
        self.assertEqual(data['promo_discount_piastres'],2000)
        self.assertEqual(data['promo_shipping_discount_piastres'],0)
        with patch('safka_auto_orders.submit_payload',
                   return_value=OrderSubmissionOutcome('accepted','offline-id')) as send:
            response=self.client.post('/checkout',data=self.delivery)
        self.assertIn('/order/success/',response.location)
        send.assert_called_once()
        with store.db() as conn:
            order=conn.execute('SELECT * FROM orders').fetchone()
            self.assertEqual(order['promo_kind'],'fixed_order')
            self.assertEqual(order['promo_value'],2000)
            self.assertEqual(order['promo_discount'],2000)
            preview=prepare_dry_run(conn,order['id'])
            self.assertEqual(preview['customer_products_piastres'],
                             data['merchandise_total_piastres']-2000)
            self.assertEqual(preview['total_to_collect_piastres'],order['total'])


class PromoNoRefreshTests(StoreTests):
    def setUp(self):
        super().setUp()
        now=int(time.time())
        with store.db() as conn:
            create_code(conn,'FREE50',0,2,now-100,now+3600,
                        kind='free_shipping',min_items=1,now=now)

    def test_apply_and_remove_via_json_keep_customer_session_and_never_redirect(self):
        with self.client.session_transaction() as session:
            session['checkout_address_draft']='test address stays intact'
            session['cart']={'sample':'example'}
        apply=self.client.post('/promo-code/apply',data={
            'csrf':self.token(),'action':'apply','promo_code':'free50','return_to':'/checkout'
        },headers={'Accept':'application/json','X-Requested-With':'XMLHttpRequest'})
        self.assertEqual(apply.status_code,200)
        self.assertEqual(apply.json['promo_state']['kind'],'free_shipping')
        self.assertTrue(apply.json['ok'])
        self.assertNotIn('Location',apply.headers)
        with self.client.session_transaction() as session:
            self.assertEqual(session['checkout_address_draft'],'test address stays intact')
            self.assertEqual(session['cart'],{'sample':'example'})
            self.assertEqual(session['promo_code'],'FREE50')
        remove=self.client.post('/promo-code/apply',data={
            'csrf':self.token(),'action':'remove','return_to':'/checkout'
        },headers={'Accept':'application/json'})
        self.assertEqual(remove.status_code,200)
        self.assertIsNone(remove.json['promo_state'])
        self.assertNotIn('Location',remove.headers)
        with self.client.session_transaction() as session:
            self.assertEqual(session['checkout_address_draft'],'test address stays intact')
            self.assertNotIn('promo_code',session)
        with store.db() as conn:
            self.assertEqual(conn.execute("SELECT used_count FROM promo_codes WHERE code='FREE50'").fetchone()[0],0)

    def test_bad_ajax_code_reports_error_without_replacing_previous_discount(self):
        self.client.post('/promo-code/apply',data={
            'csrf':self.token(),'promo_code':'FREE50'},headers={'Accept':'application/json'})
        failed=self.client.post('/promo-code/apply',data={
            'csrf':self.token(),'promo_code':'NO-SUCH-CODE'},headers={'Accept':'application/json'})
        self.assertEqual(failed.status_code,422)
        self.assertFalse(failed.json['ok'])
        with self.client.session_transaction() as session:
            self.assertEqual(session['promo_code'],'FREE50')

    def test_customer_promo_markup_has_ajax_apply_remove_forms_without_nested_forms(self):
        for url in ('/promo-code','/cart'):
            response=self.client.get(url)
            self.assertEqual(response.status_code,200)
            self.assertIn(b'data-async-promo="customer"',response.data)
            self.assertIn(b'data-promo-remove',response.data)
            self.assertIn(b'promo.js',response.data)
        checkout_template=store.app.jinja_env.get_template('checkout.html')
        self.assertIn('promo:changed',checkout_template.render.__code__.co_name if False else
                      checkout_template.environment.loader.get_source(store.app.jinja_env,'checkout.html')[0])
        self.assertIn('data-async-promo="customer"',
                      checkout_template.environment.loader.get_source(store.app.jinja_env,'checkout.html')[0])

    def test_admin_ajax_creates_and_disables_free_shipping_without_redirect(self):
        self.admin_login()
        from datetime import datetime,timedelta
        from zoneinfo import ZoneInfo
        now=datetime.now(ZoneInfo('Africa/Cairo'))
        start=(now-timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M')
        end=(now+timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        page=self.client.get('/admin/promo-codes')
        self.assertEqual(page.status_code,200)
        self.assertIn(b'data-select-free-shipping',page.data)
        self.assertIn(b'data-async-promo="admin"',page.data)
        created=self.client.post('/admin/promo-codes/create',data={
            'csrf':self.token(),'code':'FREESHIP2026','kind':'free_shipping',
            'min_items':'1','max_uses':'10','starts_at':start,'expires_at':end,
        },headers={'Accept':'application/json'})
        self.assertEqual(created.status_code,200,created.get_data(as_text=True))
        self.assertTrue(created.json['ok'])
        self.assertNotIn('Location',created.headers)
        with store.db() as conn:
            created_row=conn.execute("SELECT kind,max_uses,percent,amount_piastres FROM promo_codes WHERE code='FREESHIP2026'").fetchone()
            self.assertEqual(tuple(created_row),('free_shipping',10,0,0))
        toggled=self.client.post('/admin/promo-codes/FREESHIP2026/toggle',
                     data={'csrf':self.token()},headers={'Accept':'application/json'})
        self.assertEqual(toggled.status_code,200)
        self.assertTrue(toggled.json['ok'])
        with store.db() as conn:
            self.assertEqual(conn.execute("SELECT active FROM promo_codes WHERE code='FREESHIP2026'").fetchone()[0],0)

    def test_admin_form_json_validation_preserves_submitted_values_on_error(self):
        self.admin_login()
        bad=self.client.post('/admin/promo-codes/create',data={
            'csrf':self.token(),'code':'BADFREE','kind':'free_shipping',
            'min_items':'1','max_uses':'1','starts_at':'bad','expires_at':'bad'
        },headers={'Accept':'application/json'})
        self.assertEqual(bad.status_code,422)
        self.assertFalse(bad.json['ok'])
        self.assertNotIn('Location',bad.headers)
        with store.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM promo_codes WHERE code='BADFREE'").fetchone()[0],0)


if __name__=='__main__':
    unittest.main()
