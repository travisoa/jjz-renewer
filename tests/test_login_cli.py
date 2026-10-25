from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from jjz.auth import AuthError
from jjz.cli import interactive_login, main
from jjz.config import ConfigError, Settings


class LoginCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(config_path=self.root / "config.toml", base_url="",
                                 auth_file=self.root / "secrets/auth.txt", plate="冀ATEST01",
                                 data_dir=self.root / "data")

    def test_noninteractive_login_does_not_start_network_or_prompt(self):
        gateway = Mock()
        with patch("sys.stdin.isatty", return_value=False), self.assertRaises(ConfigError):
            interactive_login(self.settings, "sms", gateway)
        gateway.preflight.assert_not_called()

    def test_certificate_failure_before_phone_password_captcha_or_sms(self):
        gateway = Mock()
        gateway.preflight.side_effect = AuthError("证书校验失败", category="certificate")
        with patch("sys.stdin.isatty", return_value=True), patch("jjz.cli.getpass") as prompt:
            with self.assertRaises(AuthError):
                interactive_login(self.settings, "sms", gateway)
        prompt.assert_not_called()
        gateway.captcha.assert_not_called()
        gateway.send_sms.assert_not_called()

    def test_user_can_cancel_before_any_personal_data(self):
        gateway = Mock()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="NO"), \
                patch("jjz.cli.getpass") as prompt, redirect_stdout(StringIO()):
            result = interactive_login(self.settings, "password", gateway)
        self.assertEqual(result["action"], "login_cancelled")
        prompt.assert_not_called()
        gateway.captcha.assert_not_called()

    def test_sms_flow_does_not_save_or_print_any_credential(self):
        gateway = Mock()
        gateway.captcha.return_value = b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE"
        gateway.login_sms.return_value = {"redirect_url": "https://bjjj.jtgl.beijing.gov.cn/?ticket=SECRET_TICKET"}
        output = StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="YES"), \
                patch("jjz.cli.getpass", side_effect=["13800000000", "ABCD", "123456"]), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(output):
            result = interactive_login(self.settings, "sms", gateway)
        gateway.send_sms.assert_called_once_with("13800000000", "ABCD")
        gateway.login_sms.assert_called_once_with("13800000000", "123456")
        self.assertFalse(result["business_auth_verified"])
        self.assertFalse(self.settings.auth_file.exists())
        self.assertEqual(list(self.settings.data_dir.iterdir()), [])
        for secret in ("13800000000", "123456", "SECRET_TICKET"):
            self.assertNotIn(secret, output.getvalue() + str(result))

    def test_preflight_cli_does_not_need_config_and_sanitizes_certificate_error(self):
        gateway = Mock()
        gateway.preflight.side_effect = AuthError("证书校验失败", category="certificate")
        output = StringIO()
        with patch("jjz.cli.LoginGateway", return_value=gateway), redirect_stdout(output):
            code = main(["--config", str(self.root / "missing.toml"), "login-check"])
        self.assertEqual(code, 2)
        self.assertIn('"category": "certificate"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
