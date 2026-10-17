"""No Flask or external API needed; does not touch actual user database."""
import json
import sqlite3
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone,timedelta

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from safka_bridge import (
    egp_to_piastres,sale_price_piastres,customer_shipping_piastres,
    normalize_product,normalize_shipping,init_safka_schema,import_products,
    import_shipping,quote_safka_shipping,queue_safka_order,order_preview,
    supplier_stock_ready,expand_safka_shipping,CatalogValidationError)
from safka_import import _extract_list
from safka_probe import preview_records


def fake_product(product_id='A',price=30,stock=5,category='إلكترونيات'):
    return {'id':product_id,'name':'منتج اختباري '+product_id,'price':price,'quantity':stock,
            'category':{'name':category},'description':'للاختبار فقط','images':['https://example.com/a.jpg']}


def database():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON')
    c.executescript('''
        CREATE TABLE products (id INTEGER PRIMARY KEY,title TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,
          description TEXT NOT NULL DEFAULT '',price INTEGER NOT NULL,image TEXT NOT NULL DEFAULT '',
          active INTEGER NOT NULL DEFAULT 1,options_json TEXT NOT NULL DEFAULT '{}',
          category TEXT NOT NULL DEFAULT '',supplier_cost INTEGER);
        CREATE TABLE product_media(id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products(id),
          kind TEXT NOT NULL, path TEXT NOT NULL, position INTEGER NOT NULL);
        CREATE TABLE orders(id INTEGER PRIMARY KEY, reference TEXT,total INTEGER,shipping_fee INTEGER,
          shipping_supplier_cost INTEGER,customer TEXT,phone TEXT,governorate TEXT,address TEXT,status TEXT NOT NULL DEFAULT 'new');
        CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL REFERENCES orders(id),
          product_id INTEGER REFERENCES products(id),title TEXT,unit_price INTEGER,unit_cost INTEGER,
          quantity INTEGER,variant_json TEXT NOT NULL DEFAULT '{}');
        INSERT INTO products(title,slug,price,active) VALUES ('مدوي','manual-prod',15000,1);
        INSERT INTO orders(id,reference,total,shipping_fee,customer,phone,governorate,address)
          VALUES (1,'SH-TEST',15000,5000,'اختبار','01000000000','الجيزة','عنوان اختبار');
    ''')
    init_safka_schema(c)
    return c


class PreviewPaginationTests(unittest.TestCase):
    def test_shipping_preview_caps_ignored_page_size(self):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, *args): return self._bytes
            _bytes = json.dumps({'success':True,'data':[{'_id':str(i),'governorateNameAr':'الجيزة','price':50,'cities':[]} for i in range(27)]}).encode()
        with patch('safka_probe._SAFE_OPENER.open',return_value=FakeResponse()):
            results=preview_records('/api/v1/public/price-list','fake-key','shipping',timeout=1)
        self.assertEqual(len(results),2)
        self.assertEqual(results[0]['_id'],'0')


class PricingTests(unittest.TestCase):
    def test_rounding_ALWAYS_up_to_next_ten(self):
        cases={30:40,50:70,80:110,100:140,150:200,200:270,31:50,34:50,51:70,101:140,105:140}
        for supplier,expected in cases.items():
            self.assertEqual(sale_price_piastres(egp_to_piastres(supplier)),expected*100)

    def test_rounding_halves_up(self):
        # 33.75 × 4/3 = 45; 45 -> 50
        self.assertEqual(sale_price_piastres(egp_to_piastres('33.75')),5000)
        self.assertEqual(egp_to_piastres('30.05'),3005)

    def test_supplier_code_kept_separate_from_provider_id(self):
        product=fake_product('ID-7')
        product['barcode']='BAR-9'
        normalized=normalize_product(product)
        self.assertEqual(normalized['supplier_id'],'ID-7')
        self.assertEqual(normalized['supplier_code'],'BAR-9')

    def test_shipping_markup_fixed(self):
        self.assertEqual(customer_shipping_piastres(egp_to_piastres('40')),5000)

    def test_untrusted_money_rejected(self):
        for invalid in ('nan','-1','Infinity',None,True):
            with self.assertRaises(CatalogValidationError):egp_to_piastres(invalid)

    def test_no_fake_supplier_inventory(self):
        normalized=normalize_product({'id':1,'name':'منتج','price':100,'category':'اختبار'})
        self.assertIsNone(normalized['stock'])

    def test_safka_public_product_list_mapping_remains_unpublished(self):
        sample = {'_id':'673a2c721548cec72da05e36', 'name':'منتج صفقة',
                  'barcode':'6RZ254b','sale_price':180,
                  'images':['https://example.com/a.jpg'],
                  'properties':[{'key':'مقاس','value':35}],
                  'is_active':True}
        mapped = normalize_product(sample)
        self.assertEqual(mapped['supplier_cost'],18000)
        self.assertEqual(mapped['category'],'منتجات متنوعة')
        self.assertIsNone(mapped['stock'])
        self.assertEqual(mapped['options'],{})
        with database() as conn:
            import_products(conn,[sample])
            imported=conn.execute("SELECT active FROM products WHERE source='safka'").fetchone()
            self.assertEqual(imported['active'],0)

    def test_safka_governorate_cities_expand_with_original_price_id(self):
        row = {'_id':'pr1','governorateNameAr':'القاهرة','price':95,
               'cities':[{'id':'1','city_name_ar':'مدينة نصر'},
                         {'id':'2','city_name_ar':'المعادي'}]}
        rates = [normalize_shipping(x) for x in expand_safka_shipping([row])]
        self.assertEqual(len(rates),2)
        self.assertEqual([x['city'] for x in rates],['مدينة نصر','المعادي'])
        self.assertEqual([x['fee'] for x in rates],[10500,10500])
        self.assertEqual([x['pricing_document_id'] for x in rates],['pr1','pr1'])

    def test_safka_city_missing_name_rejected(self):
        with self.assertRaises(CatalogValidationError):
            expand_safka_shipping([{'_id':'pr2','price':85,'cities':[{'id':'1'}]}])

    def test_safka_price_list_governorate_mapping(self):
        sample={'_id':'price-1','governorateNameAr':'الجيزة','price':45,'cities':[{'city_name_ar':'الدقي'}]}
        mapped=normalize_shipping(expand_safka_shipping([sample])[0])
        self.assertEqual(mapped['governorate'],'الجيزة')
        self.assertEqual(mapped['fee'],5500)
        self.assertEqual(mapped['pricing_document_id'],'price-1')

    def test_safka_nested_cities_must_not_be_flattened(self):
        with self.assertRaises(CatalogValidationError):
            normalize_shipping({'_id':'price-2','governorateNameAr':'الجيزة',
                                'price':45,'cities':[{'name':'الدقي','price':75}]})

    def test_variants_unknown_format_not_silently_dropped(self):
        row=fake_product()
        row['variants']=[{'id':1,'size':'S'}]
        with self.assertRaises(CatalogValidationError):normalize_product(row)

    def test_categories_preserved(self):
        p=fake_product()
        p['categories']=[{'name':'ملابس'},{'name':'أطفال'}]
        del p['category']
        self.assertEqual(normalize_product(p)['category'],'ملابس / أطفال')


class CatalogDbTests(unittest.TestCase):
    def setUp(self):self.c=database()
    def tearDown(self):self.c.close()

    def test_catalog_import_keeps_manual_data(self):
        result=import_products(self.c,[fake_product('S1',30,10)])
        self.assertEqual(result['updated'],1)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM products WHERE source='manual'").fetchone()[0],1)
        imported=self.c.execute("SELECT * FROM products WHERE source='safka'").fetchone()
        self.assertEqual(imported['supplier_cost'],3000)
        self.assertEqual(imported['price'],4000)
        self.assertEqual(imported['supplier_stock'],10)
        self.assertEqual(imported['category'],'إلكترونيات')
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM orders').fetchone()[0],1)
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM product_media').fetchone()[0],1)

    def test_product_id_stable_and_stock_updated(self):
        import_products(self.c,[fake_product('S1',30,10)])
        prior=self.c.execute("SELECT * FROM products WHERE source='safka'").fetchone()
        import_products(self.c,[fake_product('S1',150,0)])
        after=self.c.execute("SELECT * FROM products WHERE source='safka'").fetchone()
        self.assertEqual(after['id'],prior['id'])
        self.assertEqual(after['slug'],prior['slug'])
        self.assertEqual(after['price'],20000)
        self.assertEqual(after['active'],0)
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM products WHERE source='safka'").fetchone()[0],1)

    def test_missing_stock_hidden(self):
        p=fake_product('S2')
        del p['quantity']
        import_products(self.c,[p])
        row=self.c.execute("SELECT * FROM products WHERE source='safka'").fetchone()
        self.assertEqual(row['active'],0)
        self.assertFalse(supplier_stock_ready(row))

    def test_invalid_catalog_atomic(self):
        with self.assertRaises(CatalogValidationError):
            import_products(self.c,[fake_product('S1'),{'id':'S2','name':'bad'}])
        self.assertEqual(self.c.execute("SELECT COUNT(*) FROM products WHERE source='safka'").fetchone()[0],0)

    def test_full_catalog_hides_removed_supplier_items_only(self):
        import_products(self.c,[fake_product('S1'),fake_product('S2')],complete=True)
        result=import_products(self.c,[fake_product('S1')],complete=True)
        self.assertEqual(result['hidden'],1)
        self.assertEqual(self.c.execute("SELECT active FROM products WHERE supplier_product_id='S2'").fetchone()[0],0)
        self.assertEqual(self.c.execute("SELECT active FROM products WHERE slug='manual-prod'").fetchone()[0],1)

    def test_empty_full_catalog_refused(self):
        with self.assertRaises(CatalogValidationError):import_products(self.c,[],complete=True)

    def test_shipping_price_plus_10_and_old_order_snapshot(self):
        import_shipping(self.c,[{'governorate':'الجيزة','shipping_fee':40},
                                {'governorate':'الجيزة','city':'الدقي','shipping_fee':42}])
        self.assertEqual(quote_safka_shipping(self.c,'الجيزة','الدقي','شارع 1'),(5200,4200))
        self.assertEqual(quote_safka_shipping(self.c,'الجيزة','أكتوبر',''),(5000,4000))
        self.assertEqual(self.c.execute('SELECT shipping_fee FROM orders WHERE id=1').fetchone()[0],5000)

    def test_shipping_malformed_does_not_destroy_existing(self):
        import_shipping(self.c,[{'governorate':'الجيزة','shipping_fee':40}])
        with self.assertRaises(CatalogValidationError):
            import_shipping(self.c,[{'governorate':'الجيزة','shipping_fee':40},{'governorate':'مجهول'}])
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM safka_shipping_rates').fetchone()[0],1)

    def test_outbox_once_and_private_preview(self):
        import_products(self.c,[fake_product('S1')])
        pid=self.c.execute("SELECT id FROM products WHERE source='safka'").fetchone()[0]
        self.c.execute('INSERT INTO order_items(order_id,product_id,title,unit_price,unit_cost,quantity,variant_json) VALUES(?,?,?,?,?,?,?)',
                        (1,pid,'منتج اختباري S1',4000,3000,2,'{}'))
        self.assertTrue(queue_safka_order(self.c,1))
        self.assertTrue(queue_safka_order(self.c,1))
        self.assertEqual(self.c.execute('SELECT COUNT(*) FROM safka_order_outbox').fetchone()[0],1)
        preview=order_preview(self.c,1)
        self.assertEqual(preview['items'][0]['supplier_product_id'],'S1')
        self.assertEqual(preview['customer'],'اختبار')

    def test_missing_source_product_does_not_queue(self):
        self.c.execute('INSERT INTO order_items(order_id,product_id,title,quantity) VALUES(?,?,?,?)',(1,1,'مدوي',1))
        self.assertFalse(queue_safka_order(self.c,1))

    def test_stock_stale_blocks(self):
        import_products(self.c,[fake_product('S1')])
        p=self.c.execute("SELECT * FROM products WHERE source='safka'").fetchone()
        self.assertTrue(supplier_stock_ready(p,1))
        self.assertFalse(supplier_stock_ready(p,500))
        self.c.execute('UPDATE products SET supplier_last_sync=? WHERE id=?',
                       ((datetime.now(timezone.utc)-timedelta(hours=3)).isoformat(),p['id']))
        self.assertFalse(supplier_stock_ready(self.c.execute('SELECT * FROM products WHERE id=?',(p['id'],)).fetchone()))

    def test_response_list_shapes(self):
        self.assertEqual(_extract_list({'data':{'products':[1,2]}},'products'),[1,2])


if __name__=='__main__':unittest.main()

class FutureOrderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.c=database()
        import_products(self.c,[fake_product('S1')])
        pid=self.c.execute("SELECT id FROM products WHERE source='safka'").fetchone()[0]
        self.c.execute('INSERT INTO order_items(order_id,product_id,title,unit_price,unit_cost,quantity,variant_json) VALUES(?,?,?,?,?,?,?)',
                       (1,pid,'منتج اختباري',4000,3000,1,'{}'))
        queue_safka_order(self.c,1)
    def tearDown(self):self.c.close()
    def test_no_transmission_when_disabled(self):
        from safka_orders import submit_order_once
        counter=[]
        self.assertEqual(submit_order_once(self.c,1,lambda p:counter.append(p),enabled=False)['status'],'blocked_v42')
        self.assertEqual(counter,[])
    def test_no_transmission_even_if_enabled_flag_true(self):
        from safka_orders import submit_order_once
        calls=[]
        self.assertEqual(submit_order_once(self.c,1,lambda p:calls.append(p),enabled=True)['status'],'blocked_v42')
        self.assertEqual(calls,[])
        self.assertEqual(self.c.execute('SELECT status FROM safka_order_outbox').fetchone()[0], 'awaiting_provider_configuration')

    def test_dry_run_good_order_and_redaction(self):
        from safka_dry_run import prepare_dry_run, public_safe_summary
        self.c.execute("UPDATE orders SET total=10000,shipping_fee=6000,shipping_supplier_cost=5000 WHERE id=1")
        preview=prepare_dry_run(self.c,1)
        self.assertEqual(preview['total_to_collect_piastres'],10000)
        self.assertEqual(preview['estimated_margin_before_costs_piastres'],2000)
        cleaned=public_safe_summary(preview)
        self.assertNotIn('customer_phone',cleaned)
        self.assertNotIn('customer_name',cleaned)
        self.assertEqual(cleaned['items'][0]['supplier_product_id'],'S1')

    def test_dry_run_rejects_mismatched_prices_shipping(self):
        from safka_dry_run import prepare_dry_run
        self.c.execute("UPDATE orders SET total=10000,shipping_fee=5000,shipping_supplier_cost=5000 WHERE id=1")
        with self.assertRaises(CatalogValidationError):prepare_dry_run(self.c,1)

    def test_roundup_migration_preserves_old_order_snapshots(self):
        self.c.execute("UPDATE products SET supplier_cost=10000,price=13000 WHERE source='safka'")
        self.c.execute("UPDATE order_items SET unit_price=13000")
        self.c.execute("DELETE FROM safka_schema_meta WHERE key='product_price_rule'")
        init_safka_schema(self.c)
        self.assertEqual(self.c.execute("SELECT price FROM products WHERE source='safka'").fetchone()[0],14000)
        self.assertEqual(self.c.execute("SELECT unit_price FROM order_items").fetchone()[0],13000)
        init_safka_schema(self.c)
        self.assertEqual(self.c.execute("SELECT price FROM products WHERE source='safka'").fetchone()[0],14000)

if __name__=='__main__': unittest.main()
