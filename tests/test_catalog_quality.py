"""Quality and merchant-override tests: offline, no external supplier requests."""
import sqlite3
import unittest
from catalog_quality import clean_description, extract_rating, suggested_category, availability_from_source
from safka_bridge import init_safka_schema, import_products
from safka_admin_sync import checked_shipping

class CatalogQualityTests(unittest.TestCase):
 def test_html_and_script_removed(self):
  source='<h3>وصف المنتج</h3><style>.app{color:red}</style><script>alert("X")</script><p>مميزات أصلية</p>'
  cleaned=clean_description(source)
  self.assertIn('مميزات أصلية',cleaned)
  self.assertNotIn('alert',cleaned)
  self.assertNotIn('color:red',cleaned)
  self.assertNotIn('<h3>',cleaned)
 def test_rating_is_not_invented(self):
  self.assertEqual(extract_rating({'is_active':True}),(None,None))
  self.assertEqual(extract_rating({'rating':4.6,'review_count':12}),(4.6,12))
  self.assertEqual(extract_rating({'rating':5,'review_count':0}),(None,None))
 def test_listing_not_fabricated_stock(self):
  self.assertIs(availability_from_source({'is_active':True},None),True)
  self.assertIs(availability_from_source({'is_active':False},4),False)
  self.assertIsNone(availability_from_source({},None))
  self.assertEqual(suggested_category('سماعة بلوتوث'),'إلكترونيات وإكسسوارات')
 def test_shipping_items_count_verified(self):
  source={'_id':'abc123456789','governorateNameAr':'الجيزة','price':95,'is_active':True,
          'cities':[{'city_name_ar':'6 أكتوبر'}]}
  self.assertEqual(len(checked_shipping({'itemsCount':1,'pages':3},[source])),1)
  with self.assertRaises(ValueError):checked_shipping({'itemsCount':27},[source])
 def test_merchant_override_survives_supplier_resync(self):
  conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
  conn.executescript("""CREATE TABLE products(id INTEGER PRIMARY KEY,title TEXT,slug TEXT UNIQUE,description TEXT,
    price INTEGER,image TEXT,active INTEGER,options_json TEXT,category TEXT,supplier_cost INTEGER);
    CREATE TABLE product_media(id INTEGER PRIMARY KEY,product_id INTEGER,kind TEXT,path TEXT,position INTEGER);""")
  init_safka_schema(conn)
  supplier={'_id':'supplier123','name':'سماعة صفقة','sale_price':100,'is_active':True,
            'description':'<p>وصف صفقة</p>'}
  import_products(conn,[supplier])
  row=conn.execute("SELECT id FROM products WHERE source='safka'").fetchone()
  conn.execute("INSERT INTO product_overrides(product_id,title,description,category,price,options_json) VALUES(?,?,?,?,?,?)",
               (row['id'],'عنوان التاجر','وصف خاص','إلكترونيات وإكسسوارات',19900,'{}'))
  supplier['name']='اسم جديد من المصدر';supplier['sale_price']=110
  import_products(conn,[supplier])
  saved=conn.execute("SELECT title,description,price,supplier_cost FROM products WHERE id=?",(row['id'],)).fetchone()
  self.assertEqual((saved['title'],saved['description'],saved['price']),('عنوان التاجر','وصف خاص',19900))
  self.assertEqual(saved['supplier_cost'],11000)
  conn.close()

if __name__=='__main__':unittest.main()
