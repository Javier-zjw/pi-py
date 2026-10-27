import unittest

from pagination import page_count, paginate


class PaginationTest(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(paginate(list(range(10)), 1, 3), [0, 1, 2])

    def test_last_page(self):
        self.assertEqual(paginate(list(range(10)), 4, 3), [9])

    def test_page_count(self):
        self.assertEqual(page_count(10, 5), 2)
        self.assertEqual(page_count(11, 5), 3)


if __name__ == "__main__":
    unittest.main()
