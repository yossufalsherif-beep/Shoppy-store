"""Offline tests for Safka admin synchronization; no actual API or DB credentials."""
import unittest
import sqlite3
from unittest.mock import patch, MagicMock
from safka_admin_sync import api_page, checked_products, checked_shipping, sync_product_page
from safka_bridge import CatalogValidationError


class SafkaAdminSyncTests(unittest.TestCase):
    def test_product_pagination_requires_verified_current_page(self):
        rows=[{'_id':'p1','name':'منتج','sale_price':100,'images':[]}]
        self.assertEqual(checked_products({'currentPage':1,'pages':26},rows,1),(26,1))
        with self.assertRaises(CatalogValidationError):
            checked_products({'currentPage':2,'pages':26},rows,1)

    def test_shipping_requires_complete_governorate_snapshot(self):
        sample={'_id':'x','governorateNameAr':'القاهرة','price':95,
                'cities':[{'id':'1','city_name_ar':'المعادي'}]}
        with self.assertRaises(CatalogValidationError):
            checked_shipping({'totalActiveGovernorates':27},[sample])
        rates=checked_shipping({'totalActiveGovernorates':1},[sample])
        self.assertEqual(len(rates),1)
        self.assertEqual(rates[0]['city'],'المعادي')

    def test_city_rate_missing_name_is_not_accepted(self):
        sample={'_id':'x','governorateNameAr':'القاهرة','price':95,
                'cities':[{'id':'1'}]}
        with self.assertRaises(CatalogValidationError):
            checked_shipping({'totalActiveGovernorates':1},[sample])

    def test_bounded_fixed_api_path(self):
        with self.assertRaises(CatalogValidationError):
            api_page('private','https://evil.invalid/api/v1/public/products','products')
        with self.assertRaises(CatalogValidationError):
            api_page('private','/api/v1/public/products?token=private','products')

    def test_complete_catalog_soft_archives_removed_only_after_last_page(self):
        from safka_bridge import init_safka_schema
        conn=sqlite3.connect(':memory:')
        conn.row_factory=sqlite3.Row
        conn.executescript("""
        CREATE TABLE products(id INTEGER PRIMARY KEY,title TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,
           description TEXT NOT NULL DEFAULT '',price INTEGER NOT NULL,image TEXT NOT NULL DEFAULT '',
           active INTEGER NOT NULL DEFAULT 1,options_json TEXT NOT NULL DEFAULT '{}',
           category TEXT NOT NULL DEFAULT '',supplier_cost INTEGER);
        CREATE TABLE orders(id INTEGER PRIMARY KEY,reference TEXT,shipping_fee INTEGER,total INTEGER);
        CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER,product_id INTEGER,
           title TEXT,unit_price INTEGER,quantity INTEGER);
        CREATE TABLE product_media(id INTEGER PRIMARY KEY,product_id INTEGER,kind TEXT,
           path TEXT,position INTEGER);
        CREATE TABLE store_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '');
        INSERT INTO products(title,slug,price,active) VALUES('Manual','manual',100,1);
        """)
        init_safka_schema(conn)
        try:
            p1={'_id':'safka1','name':'منتج واحد','sale_price':100,'is_active':True}
            p2={'_id':'safka2','name':'منتج تاني','sale_price':120,'is_active':True}
            first=sync_product_page(conn,[p1],1,2)
            self.assertEqual(first['next_page'],2)
            self.assertEqual(first['archived'],0)
            second=sync_product_page(conn,[p2],2,2)
            self.assertTrue(second['cycle_complete'])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE source='safka' AND supplier_listed=1").fetchone()[0],2)
            # A new full scan returns only p1. Historic p2 stays in database.
            third=sync_product_page(conn,[p1],1,1)
            self.assertEqual(third['archived'],1)
            self.assertEqual(conn.execute("SELECT supplier_listed FROM products WHERE supplier_product_id='safka2'").fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT active FROM products WHERE slug='manual'").fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM products WHERE source='safka'").fetchone()[0],2)
        finally:
            conn.close()

if __name__ == '__main__':
    unittest.main()
