"""Tests for the Safka Create Order screenshots, using mock transport ONLY."""
import json
import sqlite3
import unittest
from pathlib import Path
from safka_live_orders import canonical_order, build_payload, load_contract, OrderContractError, submit_payload
from safka_bridge import init_safka_schema, import_shipping
from tests.test_safka_v43_order_adapter import PREVIEW, DummyOpener


class DocumentedCreateOrder(unittest.TestCase):
    def setUp(self):
        self.contract = load_contract(str(Path(__file__).resolve().parents[1] / 'safka_documented_create_order.json'))

    def complete(self):
        p = dict(PREVIEW)
        p['shipping_pricing_document_id'] = '64878fe6dc16090c1858e698'
        p['supplier_city_id'] = '336'
        return canonical_order(p, commission_egp=90.0, property_ids={0:'695d073bddc488a504bb2d50'})

    def test_exact_documented_fields(self):
        self.assertEqual(self.contract['path'], '/api/v1/public/orders')
        payload = build_payload(self.complete(), self.contract)
        self.assertEqual(set(payload), {'client_name','client_phone1','client_address','shipping_governorate','city','commission','total','note','items'})
        self.assertEqual(payload['city'],'336')
        self.assertEqual(payload['shipping_governorate'],'64878fe6dc16090c1858e698')
        self.assertEqual(payload['total'],190.0)
        self.assertEqual(payload['commission'],90.0)
        self.assertEqual(payload['items'],[{'product':'A-1','property':'695d073bddc488a504bb2d50','qty':'1'}])
        self.assertNotIn('governorate',payload)
        self.assertNotIn('shipping',payload)

    def test_missing_commission_blocks_send(self):
        payload=self.complete();payload['requested_commission_egp']=None
        with self.assertRaises(OrderContractError):build_payload(payload,self.contract)

    def test_missing_property_blocks_send(self):
        payload=self.complete();payload['items'][0]['supplier_property_id']=None
        with self.assertRaises(OrderContractError):build_payload(payload,self.contract)

    def test_missing_pricing_document_blocks_send(self):
        payload=self.complete();payload['shipping_pricing_document_id']=None
        with self.assertRaises(OrderContractError):build_payload(payload,self.contract)

    def test_unknown_supplier_choice_blocks_send(self):
        payload=self.complete();payload['items'][0]['choices']={'size':'L'}
        payload['items'][0]['supplier_property_id']=None
        with self.assertRaises(OrderContractError):build_payload(payload,self.contract)

    def test_success_data_id_parsed_and_exact_one_post(self):
        dummy=DummyOpener({'success':True,'data':{'_id':'6966a3b27fd9c61da95eeb94','status':'pending'}})
        result=submit_payload(self.complete(),self.contract,'abcdefghijklmnop',opener=dummy)
        self.assertEqual(result.state,'accepted')
        self.assertEqual(result.provider_order_id,'6966a3b27fd9c61da95eeb94')
        self.assertEqual(len(dummy.calls),1)
        self.assertEqual(dummy.calls[0].full_url,'https://api.safka-eg.com/api/v1/public/orders')
        posted=json.loads(dummy.calls[0].data)
        self.assertEqual(posted['items'][0]['qty'],'1')

    def test_normalize_shipping_id(self):
        from safka_bridge import normalize_shipping
        result=normalize_shipping({'governorate':'الجيزة','shipping_fee':40,'document_id':'64878fe6dc16090c1858e698'})
        self.assertEqual(result['pricing_document_id'],'64878fe6dc16090c1858e698')
        self.assertEqual(result['fee'],5000)

    def test_existing_shipping_rate_additive_migration(self):
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.executescript("""CREATE TABLE products(id INTEGER PRIMARY KEY, source TEXT NOT NULL DEFAULT 'manual',supplier_cost INTEGER,price INTEGER);
          CREATE TABLE orders(id INTEGER PRIMARY KEY, reference TEXT, customer TEXT,phone TEXT,governorate TEXT,address TEXT,
            shipping_fee INTEGER,shipping_supplier_cost INTEGER,total INTEGER);
          CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,title TEXT,
            quantity INTEGER,unit_price INTEGER,unit_cost INTEGER,variant_json TEXT);""")
        conn.execute('INSERT INTO orders VALUES(1,?,?,?,?,?,?,?,?)',('T-1','Customer','01000000000','الجيزة','الدقي | شارع | عمارة',5000,4000,19000))
        init_safka_schema(conn)
        self.assertIn('pricing_document_id',{r['name'] for r in conn.execute('PRAGMA table_info(safka_shipping_rates)')})
        import_shipping(conn,[{'governorate':'الجيزة','city':'الدقي','area':'شارع','shipping_fee':40,'document_id':'64878fe6dc16090c1858e698'}])
        row=conn.execute('SELECT pricing_document_id FROM safka_shipping_rates').fetchone()
        self.assertEqual(row['pricing_document_id'],'64878fe6dc16090c1858e698')
        self.assertEqual(conn.execute('SELECT total FROM orders WHERE id=1').fetchone()['total'],19000)
        conn.close()


if __name__=='__main__': unittest.main()

class SubmittedCommissionSnapshot(unittest.TestCase):
    def test_owner_confirmed_commission_is_saved_before_provider_post(self):
        from safka_live_orders import claim_one_test_order
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
        conn.executescript("""
        CREATE TABLE orders(id INTEGER PRIMARY KEY,reference TEXT,status TEXT);
        CREATE TABLE safka_order_outbox(order_id INTEGER PRIMARY KEY,status TEXT,error TEXT,
          attempt_count INTEGER,updated_at TEXT,submitted_commission_piastres INTEGER);
        INSERT INTO orders VALUES(1,'SH-TEST001','new');
        INSERT INTO safka_order_outbox VALUES(1,'awaiting_provider_configuration','',0,'',NULL);
        """)
        claim_one_test_order(conn,1,'SH-TEST001',9000)
        row=conn.execute('SELECT status,submitted_commission_piastres FROM safka_order_outbox').fetchone()
        self.assertEqual(row['status'],'test_sending')
        self.assertEqual(row['submitted_commission_piastres'],9000)
        conn.close()
