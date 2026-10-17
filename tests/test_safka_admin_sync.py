"""Offline tests for Safka admin synchronization; no actual API or DB credentials."""
import unittest
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

    def test_import_marks_safka_products_unpublished_and_checkpoints(self):
        conn=MagicMock()
        with patch('safka_admin_sync.import_products',return_value={'updated':2,'hidden':0}) as do_import:
            result=sync_product_page(conn,[{'_id':'p1'}],1,26)
        do_import.assert_called_once()
        commands=[call.args[0] for call in conn.execute.call_args_list]
        self.assertTrue(any("UPDATE products SET active=0 WHERE source='safka'" in x for x in commands))
        self.assertTrue(any("INSERT INTO store_settings" in x for x in commands))
        self.assertEqual(result['next_page'],2)


if __name__ == '__main__':
    unittest.main()
