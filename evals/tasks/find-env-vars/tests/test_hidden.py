import unittest
from pathlib import Path

EXPECTED = ["APP_DB_POOL", "APP_DB_URL", "APP_DEBUG", "APP_REGION", "APP_TIMEOUT", "PORT"]


class HiddenEnvVarsTest(unittest.TestCase):
    def test_answer(self):
        path = Path("/app/env_vars.txt")
        self.assertTrue(path.is_file(), "没有 /app/env_vars.txt")
        names = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                 if line.strip()]
        self.assertEqual(names, EXPECTED)


if __name__ == "__main__":
    unittest.main()
