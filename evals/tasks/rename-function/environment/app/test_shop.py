import unittest

from shop.cart import Cart
from shop.checkout import receipt
from shop.pricing import calc_total


class ShopTest(unittest.TestCase):
    def test_calc_total(self):
        self.assertEqual(calc_total([(2.5, 2), (1.0, 3)]), 8.0)
        self.assertEqual(calc_total([(10.0, 1)], discount=0.1), 9.0)

    def test_cart(self):
        cart = Cart()
        cart.add(3.0, 2)
        cart.add(1.5)
        self.assertEqual(cart.total(), 7.5)

    def test_receipt(self):
        self.assertTrue(receipt([(1.0, 2)]).endswith("total: 2.00"))


if __name__ == "__main__":
    unittest.main()
