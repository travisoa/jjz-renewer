from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from jjz.cli import main


PLATE = "冀ATEST01"


class RemainingCountCliTests(unittest.TestCase):
    def setUp(self):
        previous_umask = os.umask(0o077)
        self.addCleanup(os.umask, previous_umask)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        auth = root / "synthetic-authorization"
        auth.write_text("SYNTHETIC_AUTHORIZATION", encoding="utf-8")
        auth.chmod(0o600)
        self.config = root / "synthetic-config.toml"
        self.config.write_text(
            f'[api]\nbase_url="https://example.invalid"\nauth_file="{auth}"\n'
            f'[vehicle]\nplate="{PLATE}"\n', encoding="utf-8")

    def status(self, remaining):
        client = Mock()
        client.state.return_value = {
            "code": 200,
            "data": {"bzclxx": [{"hphm": PLATE, "ecbzxx": None, "bzxx": None,
                                   "ylzsfkb": True, "elzsfkb": True, "sycs": remaining}]},
        }
        output = StringIO()
        with patch("jjz.cli.ApiClient", return_value=client), redirect_stdout(output):
            code = main(["--config", str(self.config), "status"])
        self.assertEqual(code, 0)
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertNotIn(PLATE, output.getvalue())
        self.assertNotIn("SYNTHETIC_AUTHORIZATION", output.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["action"], "status")
        self.assertEqual(result["can_apply"], {"inside": True, "outside": True})
        client.state.assert_called_once_with()
        client.submit.assert_not_called()
        return result["inside_remaining"]

    def test_nonnegative_integer_counts_are_preserved(self):
        for value in (0, 8, 12):
            with self.subTest(value=value):
                self.assertEqual(self.status(value), value)

    def test_ascii_digit_string_counts_are_parsed(self):
        for value, expected in (("0", 0), ("8", 8), ("12", 12), ("08", 8)):
            with self.subTest(value=value):
                self.assertEqual(self.status(value), expected)

    def test_unknown_and_invalid_counts_remain_unknown(self):
        values = (None, "", True, False, -1, "-1", 8.0, 8.5, "8.0", " 8", "+8",
                  "²", "８", "٨", "8²", [], {})
        for value in values:
            with self.subTest(value=value):
                self.assertIsNone(self.status(value))

    def test_excessively_long_digit_string_does_not_break_json_output(self):
        self.assertIsNone(self.status("8" * 5000))


if __name__ == "__main__":
    unittest.main()
