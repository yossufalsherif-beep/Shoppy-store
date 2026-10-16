import sqlite3
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from safka_bridge import sale_price_piastres, egp_to_piastres, CatalogValidationError
from safka_orders import submit_order_once

class V42GuardrailTests(unittest.TestCase):
    def test_roundup_never_down(self):
        for value in ('0.01','1','30','31','51','99.99','100','150','200','1000'):
            cost=egp_to_piastres(value)
            result=sale_price_piastres(cost)
            self.assertEqual(result%1000,0)
            self.assertGreaterEqual(result*3,cost*4)
            if result:
                self.assertLess((result-1000)*3,cost*4)

    def test_send_is_hard_blocked_even_with_callback_and_enabled(self):
        used=[]
        response=submit_order_once(None,5,provider_submit=lambda x:used.append(x),enabled=True)
        self.assertEqual(response['status'],'blocked_v42')
        self.assertEqual(used,[])

if __name__=='__main__': unittest.main()
