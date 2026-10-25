from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from jjz.auth import AuthError
from jjz.api import ApiError, BUSINESS_LOGIN_ORIGIN, UNIFIED_AUTH_CALLBACK_URL
from jjz.cli import interactive_login, main
from jjz.config import ConfigError, Settings


class LoginCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.settings = Settings(config_path=self.root / "config.toml", base_url=BUSINESS_LOGIN_ORIGIN,
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

    def _login_file(self, body):
        path = self.root / "secrets/login.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text("[login]\n" + body, encoding="utf-8")
        path.chmod(0o600)
        self.settings.login_file = path

    def _ready_login(self):
        gateway = Mock()
        gateway.captcha.return_value = b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE"
        gateway.login_sms.return_value = {"redirect_url": UNIFIED_AUTH_CALLBACK_URL + "?code=SECRET_CODE"}
        gateway.take_callback.return_value = (
            BUSINESS_LOGIN_ORIGIN + "/login_h5_byUserCenter.html?token=SECRET_TICKET&state=300001", b"",
        )
        business = Mock()
        business.exchange.return_value = "SYNTHETIC_BUSINESS_AUTH"
        state = Mock()
        state.state.return_value = {"code": 200, "data": {"bzclxx": [
            {"hphm": self.settings.plate, "ecbzxx": None, "bzxx": None},
        ]}}
        return gateway, business, state

    def test_configured_phone_and_password_login_without_agreement_prompt(self):
        gateway, business, state = self._ready_login()
        gateway.login_password.return_value = gateway.login_sms.return_value
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        prompts = []

        def capture(prompt):
            prompts.append(prompt)
            return "ABCD"

        output = StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as answers, \
                patch("jjz.cli.recognize_captcha", return_value=None), \
                patch("jjz.cli.getpass", side_effect=capture), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(output), \
                redirect_stderr(output):
            result = interactive_login(self.settings, "password", gateway)
        answers.assert_not_called()
        self.assertEqual(prompts, ["请本人识别图形验证码并输入（隐藏输入）: "])
        gateway.login_password.assert_called_once_with("13800000000", "SYNTHETIC_PASSWORD", "ABCD")
        self.assertTrue(result["business_auth_verified"])
        self.assertFalse(result["credential_saved"])
        for secret in ("13800000000", "SYNTHETIC_PASSWORD", "SECRET_TICKET", "SYNTHETIC_BUSINESS_AUTH"):
            self.assertNotIn(secret, output.getvalue() + str(result))

    def test_configured_phone_without_password_does_not_prompt(self):
        gateway = Mock()
        self._login_file('phone = "13800000000"\n')
        with patch("sys.stdin.isatty", return_value=True), \
                patch("jjz.cli.getpass") as prompt, redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                self.assertRaises(ConfigError):
            interactive_login(self.settings, "password", gateway)
        prompt.assert_not_called()
        gateway.preflight.assert_not_called()
        gateway.captcha.assert_not_called()

    def test_recognized_captcha_is_submitted_without_prompt_or_output(self):
        gateway, business, state = self._ready_login()
        gateway.login_password.return_value = gateway.login_sms.return_value
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        output = StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as answers, \
                patch("jjz.cli.recognize_captcha", return_value="ab12"), \
                patch("jjz.cli.getpass") as prompt, \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(output), \
                redirect_stderr(output):
            result = interactive_login(self.settings, "password", gateway)
        answers.assert_not_called()
        prompt.assert_not_called()
        gateway.login_password.assert_called_once_with("13800000000", "SYNTHETIC_PASSWORD", "ab12")
        self.assertNotIn("ab12", output.getvalue() + str(result))
        self.assertTrue(result["business_auth_verified"])

    def test_rejected_captcha_is_recognized_again_before_manual_entry(self):
        gateway, business, state = self._ready_login()
        gateway.login_password.side_effect = [
            AuthError("验证码错误或已过期，请重新获取并由本人输入。", category="verification"),
            gateway.login_sms.return_value,
        ]
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as answers, \
                patch("jjz.cli.recognize_captcha", side_effect=["aaaa", "bbbb"]), \
                patch("jjz.cli.getpass") as prompt, \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = interactive_login(self.settings, "password", gateway)
        answers.assert_not_called()
        prompt.assert_not_called()
        self.assertEqual(gateway.captcha.call_count, 2)
        self.assertEqual(gateway.login_password.call_args_list[1].args, ("13800000000", "SYNTHETIC_PASSWORD", "bbbb"))
        self.assertTrue(result["business_auth_verified"])

    def test_insufficient_real_name_level_is_not_retried_as_captcha(self):
        gateway, _, _ = self._ready_login()
        gateway.login_password.side_effect = AuthError(
            "当前账号实名认证等级不足，请由本人在京通 App 完成官方认证。", category="identity")
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as answers, \
                patch("jjz.cli.recognize_captcha", return_value="ab12"), \
                patch("jjz.cli.getpass") as prompt, redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                self.assertRaises(AuthError):
            interactive_login(self.settings, "password", gateway)
        answers.assert_not_called()
        prompt.assert_not_called()
        gateway.login_password.assert_called_once()

    def test_sms_flow_verifies_business_without_saving_or_printing_any_credential(self):
        gateway, business, state = self._ready_login()
        output = StringIO()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input") as answers, \
                patch("jjz.cli.recognize_captcha", return_value=None), \
                patch("jjz.cli.getpass", side_effect=["13800000000", "ABCD", "123456"]), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(output), \
                redirect_stderr(output):
            result = interactive_login(self.settings, "sms", gateway)
        answers.assert_not_called()
        gateway.send_sms.assert_called_once_with("13800000000", "ABCD")
        gateway.login_sms.assert_called_once_with("13800000000", "123456")
        self.assertTrue(result["business_auth_verified"])
        self.assertFalse(result["credential_saved"])
        business.exchange.assert_called_once_with("/login_h5_byUserCenter.html", "SECRET_TICKET", "300001")
        state.state.assert_called_once_with()
        self.assertFalse(self.settings.auth_file.exists())
        self.assertEqual(list(self.settings.data_dir.iterdir()), [])
        for secret in ("13800000000", "123456", "SECRET_TICKET", "SYNTHETIC_BUSINESS_AUTH"):
            self.assertNotIn(secret, output.getvalue() + str(result))

    def test_auth_set_login_saves_after_verification_without_confirmation(self):
        gateway, business, state = self._ready_login()
        with patch("jjz.cli.load_config", return_value=self.settings), \
                patch("jjz.cli.LoginGateway", return_value=gateway), \
                patch("sys.stdin.isatty", return_value=True), \
                patch("builtins.input") as answers, \
                patch("jjz.cli.recognize_captcha", return_value=None), \
                patch("jjz.cli.getpass", side_effect=["13800000000", "ABCD", "123456"]), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(StringIO()) as output, \
                redirect_stderr(StringIO()) as progress:
            code = main(["--config", str(self.settings.config_path), "auth-set", "--login", "--method", "sms"])
        self.assertEqual(code, 0)
        answers.assert_not_called()
        # Progress text goes to stderr; stdout is exactly one JSON line.
        self.assertIn("登录协议", progress.getvalue())
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(json.loads(lines[0])["credential_saved"])
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_BUSINESS_AUTH")
        self.assertEqual(self.settings.auth_file.stat().st_mode & 0o777, 0o600)

    def test_failed_business_verification_preserves_old_credential(self):
        gateway, business, state = self._ready_login()
        state.state.side_effect = ApiError("接口拒绝凭证", auth_expired=True)
        self.settings.auth_file.parent.mkdir()
        self.settings.auth_file.write_text("SYNTHETIC_OLD_AUTH")
        with patch("sys.stdin.isatty", return_value=True), \
                patch("jjz.cli.recognize_captcha", return_value=None), \
                patch("jjz.cli.getpass", side_effect=["13800000000", "ABCD", "123456"]), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                self.assertRaises(ApiError):
            interactive_login(self.settings, "sms", gateway, save=True)
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_OLD_AUTH")

    def test_login_without_save_keeps_existing_credential(self):
        gateway, business, state = self._ready_login()
        self.settings.auth_file.parent.mkdir()
        self.settings.auth_file.write_text("SYNTHETIC_OLD_AUTH")
        with patch("sys.stdin.isatty", return_value=True), \
                patch("jjz.cli.recognize_captcha", return_value=None), \
                patch("jjz.cli.getpass", side_effect=["13800000000", "ABCD", "123456"]), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open", return_value=True), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            result = interactive_login(self.settings, "sms", gateway)
        self.assertTrue(result["business_auth_verified"])
        self.assertFalse(result["credential_saved"])
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_OLD_AUTH")

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
