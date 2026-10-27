import re
import subprocess
import sys
import unittest
from pathlib import Path

APP = Path("/app")


class HiddenRenameTest(unittest.TestCase):
    def test_no_old_name_left(self):
        left = [str(p.relative_to(APP)) for p in APP.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
                and "calc_total" in p.read_text(encoding="utf-8", errors="ignore")]
        self.assertEqual(left, [], f"还有 calc_total：{left}")

    def test_new_name_works(self):
        from shop import pricing
        from shop.cart import Cart
        from shop.checkout import receipt

        self.assertFalse(hasattr(pricing, "calc_total"), "留了旧名字")
        self.assertEqual(pricing.order_total([(2.5, 2), (1.0, 3)]), 8.0)
        self.assertEqual(pricing.order_total([(10.0, 1)], discount=0.1), 9.0)
        self.assertEqual(pricing.order_total([(0.1, 3)]), 0.3)
        cart = Cart()
        cart.add(3.0, 2)
        self.assertEqual(cart.total(discount=0.5), 3.0)
        self.assertEqual(receipt([(1.25, 2)]), "2 x 1.25\ntotal: 2.50")

    def test_project_tests_pass(self):
        done = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=APP,
                              capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        ran = re.search(r"Ran (\d+) test", done.stderr)
        self.assertTrue(ran and int(ran.group(1)) >= 3, "项目里的测试被删了")


if __name__ == "__main__":
    unittest.main()
