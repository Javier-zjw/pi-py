import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def run(text, *args):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "input.txt"
        path.write_text(text, encoding="utf-8")
        done = subprocess.run([sys.executable, "/app/wordstat.py", str(path), *args],
                              capture_output=True, text=True, timeout=30, check=False)
    if done.returncode != 0:
        raise AssertionError(f"exit {done.returncode}: {done.stderr}")
    return done.stdout.splitlines()


class HiddenTopTest(unittest.TestCase):
    def test_example(self):
        text = "the cat sat on the mat\nThe dog ate the cat\n"
        self.assertEqual(run(text, "--top", "2"), ["lines: 2", "words: 11", "the 4", "cat 2"])

    def test_ties_alphabetical(self):
        text = "b a c b a c d\n"
        self.assertEqual(run(text, "--top", "3"), ["lines: 1", "words: 7", "a 2", "b 2", "c 2"])

    def test_case_insensitive(self):
        self.assertEqual(run("Go GO go stop\n", "--top", "1"), ["lines: 1", "words: 4", "go 3"])

    def test_fewer_words_than_n(self):
        self.assertEqual(run("x y\n", "--top", "5"), ["lines: 1", "words: 2", "x 1", "y 1"])

    def test_without_option_unchanged(self):
        self.assertEqual(run("one two\nthree\n"), ["lines: 2", "words: 3"])


if __name__ == "__main__":
    unittest.main()
