import unittest

from duration import parse_duration


class HiddenDurationTest(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("2D 1s"), 172801)

    def test_more(self):
        cases = {"0s": 0, "1d": 86400, "1h 5m 10s": 3910, "10s5m": 310, "  3M ": 180,
                 "1d2h3m4s": 93784, "100m": 6000, "1H": 3600}
        for text, seconds in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_duration(text), seconds)
                self.assertIsInstance(parse_duration(text), int)

    def test_invalid(self):
        for text in ("", "   ", "10", "5x", "1h1h", "1h 2H", "h", "1.5h", "-1h", "1h,2m", "1 h x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)


if __name__ == "__main__":
    unittest.main()
