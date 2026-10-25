from pathlib import Path
import tempfile
import unittest

from jjz.api import ApiError
from jjz.config import Settings
from jjz.credentials import save_authorization


TOKEN = "SYNTHETIC_BUSINESS_AUTH"


class SaveAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(config_path=self.root / "config.toml", base_url="https://service.example.invalid",
                                 auth_file=self.root / "secrets/authorization.txt", plate="冀ATEST01")

    def test_atomic_replace_is_private_and_leaves_no_temporary_file(self):
        self.settings.auth_file.parent.mkdir()
        self.settings.auth_file.write_text("SYNTHETIC_OLD_AUTH")
        save_authorization(self.settings, TOKEN)
        self.assertEqual(self.settings.read_authorization(), TOKEN)
        self.assertEqual(self.settings.auth_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.settings.auth_file.parent.iterdir()), [self.settings.auth_file])

    def test_invalid_value_keeps_previous_credential(self):
        self.settings.auth_file.parent.mkdir()
        self.settings.auth_file.write_text("SYNTHETIC_OLD_AUTH")
        for value in ("", " SYNTHETIC", "SYNTHETIC\r\nOther: value", "汉字"):
            with self.subTest(value=value), self.assertRaises(ApiError) as error:
                save_authorization(self.settings, value)
            self.assertNotIn("SYNTHETIC", str(error.exception))
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_OLD_AUTH")
        self.assertEqual(list(self.settings.auth_file.parent.iterdir()), [self.settings.auth_file])


if __name__ == "__main__":
    unittest.main()
