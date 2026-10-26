from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import fcntl
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from jjz.api import ApiError, BUSINESS_LOGIN_ORIGIN, UNIFIED_AUTH_CALLBACK_URL
from jjz.auth import AuthError
from jjz.cli import main, unattended_login
from jjz.config import ConfigError, Settings, load_config
from jjz.domain import DomainError


PLATE = "冀ATEST01"
STATE = {"code": 200, "data": {"bzclxx": [{"hphm": PLATE, "ecbzxx": None, "bzxx": None,
                                            "ylzsfkb": True, "elzsfkb": True, "sycs": 8}]}}


class UnattendedLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.settings = Settings(config_path=self.root / "config.toml", base_url=BUSINESS_LOGIN_ORIGIN,
                                 auth_file=self.root / "secrets/auth.txt", plate=PLATE,
                                 data_dir=self.root / "data", login_file=self.root / "secrets/login.toml",
                                 auto_relogin=True)
        self.settings.auth_file.parent.mkdir()
        self.settings.auth_file.write_text("SYNTHETIC_OLD_AUTH")
        self.settings.auth_file.chmod(0o600)

    def _login_file(self, body):
        self.settings.login_file.write_text("[login]\n" + body, encoding="utf-8")
        self.settings.login_file.chmod(0o600)

    def _gateway(self):
        gateway = Mock()
        gateway.captcha.return_value = b"\x89PNG\r\n\x1a\nSYNTHETIC_IMAGE"
        gateway.login_password.return_value = {"redirect_url": UNIFIED_AUTH_CALLBACK_URL + "?code=SECRET_CODE"}
        gateway.take_callback.return_value = (
            BUSINESS_LOGIN_ORIGIN + "/login_h5_byUserCenter.html?token=SECRET_TICKET&state=300001", b"",
        )
        business = Mock()
        business.exchange.return_value = "SYNTHETIC_NEW_AUTH"
        state = Mock()
        state.state.return_value = STATE
        return gateway, business, state

    def _login(self, gateway, business, state, guesses):
        output = StringIO()
        with patch("sys.stdin.isatty", return_value=False), patch("jjz.cli.getpass") as prompt, \
                patch("jjz.cli.recognize_captcha", side_effect=guesses), \
                patch("jjz.cli.BusinessLoginClient", return_value=business), \
                patch("jjz.credentials.ApiClient", return_value=state), \
                patch("jjz.cli.webbrowser.open") as browser, redirect_stdout(output), redirect_stderr(output):
            try:
                return unattended_login(self.settings, gateway)
            finally:
                prompt.assert_not_called()
                browser.assert_not_called()
                for secret in ("13800000000", "SYNTHETIC_PASSWORD", "SECRET_TICKET", "SYNTHETIC_NEW_AUTH"):
                    self.assertNotIn(secret, output.getvalue())

    def test_recognized_captcha_saves_verified_credential_without_a_terminal(self):
        gateway, business, state = self._gateway()
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        result = self._login(gateway, business, state, ["ab12"])
        gateway.login_password.assert_called_once_with("13800000000", "SYNTHETIC_PASSWORD", "ab12")
        self.assertTrue(result["credential_saved"])
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_NEW_AUTH")
        self.assertEqual(self.settings.auth_file.stat().st_mode & 0o777, 0o600)

    def test_unrecognized_captcha_stops_without_prompt_or_login_attempt(self):
        gateway, business, state = self._gateway()
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        with self.assertRaises(AuthError) as raised:
            self._login(gateway, business, state, [None, None, None])
        self.assertEqual(raised.exception.category, "verification")
        self.assertEqual(gateway.captcha.call_count, 3)
        gateway.login_password.assert_not_called()
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_OLD_AUTH")

    def test_three_rejected_guesses_stop_without_prompt(self):
        gateway, business, state = self._gateway()
        gateway.login_password.side_effect = AuthError("验证码错误", category="verification")
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        with self.assertRaises(AuthError):
            self._login(gateway, business, state, ["aaaa", "bbbb", "cccc"])
        self.assertEqual(gateway.login_password.call_count, 3)
        self.assertEqual(self.settings.auth_file.read_text(), "SYNTHETIC_OLD_AUTH")

    def test_account_error_is_not_retried(self):
        gateway, business, state = self._gateway()
        gateway.login_password.side_effect = AuthError("账号或密码错误", category="credentials")
        self._login_file('phone = "13800000000"\npassword = "SYNTHETIC_PASSWORD"\n')
        with self.assertRaises(AuthError):
            self._login(gateway, business, state, ["ab12"])
        gateway.login_password.assert_called_once()

    def test_missing_password_stops_before_network(self):
        gateway, business, state = self._gateway()
        self._login_file('phone = "13800000000"\n')
        with self.assertRaises(ConfigError):
            self._login(gateway, business, state, [])
        gateway.preflight.assert_not_called()


class AutoReloginCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        auth = root / "synthetic-authorization"
        auth.write_text("SYNTHETIC_AUTHORIZATION", encoding="utf-8")
        auth.chmod(0o600)
        self.config = root / "synthetic-config.toml"
        self.write_config(auth, True)
        self.auth = auth

    def write_config(self, auth, enabled):
        self.config.write_text(
            f'[api]\nbase_url="https://example.invalid"\nauth_file="{auth}"\n'
            f'auto_relogin={"true" if enabled else "false"}\n'
            f'[vehicle]\nplate="{PLATE}"\n', encoding="utf-8")

    def cli(self, *args, login=None, execute=None, states=()):
        client = Mock()
        client.state.side_effect = states if callable(states) else list(states)
        output = StringIO()
        self.progress = StringIO()
        with patch("jjz.cli.ApiClient", return_value=client), \
                patch("jjz.cli.unattended_login", side_effect=login) as relogin, \
                patch("jjz.cli.execute", side_effect=execute) as run, \
                redirect_stdout(output), redirect_stderr(self.progress):
            code = main(["--config", str(self.config), *args])
        return code, json.loads(output.getvalue()), relogin, run, client

    def test_status_logs_in_once_and_queries_again(self):
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        code, result, relogin, _, client = self.cli("status", states=[expired, STATE])
        self.assertEqual(code, 0)
        self.assertEqual(result["action"], "status")
        self.assertEqual(result["relogin"], "succeeded")
        relogin.assert_called_once()
        self.assertEqual(client.state.call_count, 2)

    def test_login_holds_the_shared_credential_lock(self):
        lock = self.auth.with_name(self.auth.name + ".lock")

        def login(settings):
            fd = os.open(lock, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        code, result, relogin, _, _ = self.cli("status", login=login, states=[expired, STATE])
        self.assertEqual(code, 0)
        self.assertEqual(result["relogin"], "succeeded")
        relogin.assert_called_once()
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_waiting_for_another_configs_login_stops_at_the_limit(self):
        lock = self.auth.with_name(self.auth.name + ".lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)  # another config is still logging in
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        with patch("jjz.storage.CREDENTIAL_WAIT_SECONDS", 0.3):
            code, result, relogin, _, _ = self.cli("status", states=[expired])
        self.assertEqual(code, 2)
        self.assertEqual(result["category"], "auth_expired")
        self.assertEqual(result["relogin"], "failed")
        self.assertIn("超时", result["reason"])
        relogin.assert_not_called()
        self.assertIn("等待", self.progress.getvalue())

    def test_credential_saved_by_another_config_is_used_without_logging_in(self):
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        queries = []

        def query():
            queries.append(None)
            if len(queries) > 1:
                return STATE
            # Another vehicle's config replaces the shared credential meanwhile.
            replacement = self.auth.with_name("replacement")
            replacement.write_text("SYNTHETIC_NEW_AUTHORIZATION", encoding="utf-8")
            os.replace(replacement, self.auth)
            raise expired

        code, result, relogin, _, client = self.cli("status", states=query)
        self.assertEqual(code, 0)
        self.assertEqual(result["action"], "status")
        self.assertEqual(result["relogin"], "shared")
        relogin.assert_not_called()
        self.assertEqual(client.state.call_count, 2)

    def test_disabled_switch_keeps_reporting_expired_credential(self):
        self.write_config(self.auth, False)
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        code, result, relogin, _, client = self.cli("status", states=[expired])
        self.assertEqual(code, 2)
        self.assertEqual(result["category"], "auth_expired")
        self.assertNotIn("relogin", result)
        relogin.assert_not_called()

    def test_failed_login_reports_expired_credential_and_reason(self):
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        failure = AuthError("本机未能识别图形验证码，自动重新登录已停止。", category="verification")
        code, result, relogin, _, client = self.cli("status", login=failure, states=[expired])
        self.assertEqual(code, 2)
        self.assertEqual(result["action"], "error")
        self.assertEqual(result["category"], "auth_expired")
        self.assertEqual(result["relogin"], "failed")
        self.assertIn("本机未能识别图形验证码", result["reason"])
        self.assertEqual(client.state.call_count, 1)

    def test_login_that_cannot_find_the_vehicle_reports_failed_relogin(self):
        # For example the saved login belongs to another account: login works, the vehicle is missing.
        expired = ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        mismatch = DomainError("接口中未找到目标车辆，请检查车牌及当前登录账号")
        code, result, relogin, _, client = self.cli("status", login=mismatch, states=[expired])
        self.assertEqual(code, 2)
        self.assertEqual(result["category"], "auth_expired")
        self.assertEqual(result["relogin"], "failed")
        self.assertIn("接口中未找到目标车辆", result["reason"])
        self.assertEqual(client.state.call_count, 1)

    def test_other_errors_do_not_trigger_login(self):
        offline = ApiError("接口网络请求失败或超时。")
        code, result, relogin, _, _ = self.cli("status", states=[offline])
        self.assertEqual(code, 2)
        self.assertNotIn("category", result)
        relogin.assert_not_called()

    def test_run_retries_after_credential_rejected_before_journaling(self):
        blocked = {"action": "blocked", "reason": "提交前校验未通过：接口拒绝凭证", "category": "auth_expired",
                   "vehicle": "冀A***01", "permit": "outside"}
        preview = {"action": "preview", "server_checks": "passed", "reason": "本次只预览",
                   "vehicle": "冀A***01", "permit": "outside"}
        code, result, relogin, run, _ = self.cli("run", execute=[blocked, preview])
        self.assertEqual(code, 0)
        self.assertEqual(result["action"], "preview")
        self.assertEqual(result["relogin"], "succeeded")
        relogin.assert_called_once()
        self.assertEqual(run.call_count, 2)

    def test_run_never_logs_in_again_after_a_journaled_attempt(self):
        rejected = {"action": "rejected", "attempt": "SYNTHETIC_KEY", "category": "auth_expired",
                    "reason": "接口拒绝凭证", "vehicle": "冀A***01", "permit": "outside"}
        code, result, relogin, run, _ = self.cli("run", "--submit", execute=[rejected])
        self.assertEqual(code, 2)
        relogin.assert_not_called()
        run.assert_called_once()

    def test_doctor_checks_login_file_only_when_enabled(self):
        output = StringIO()
        with redirect_stdout(output):
            main(["--config", str(self.config), "doctor"])
        self.assertIn("auto_relogin", json.loads(output.getvalue())["checks"])
        self.write_config(self.auth, False)
        output = StringIO()
        with redirect_stdout(output):
            main(["--config", str(self.config), "doctor"])
        self.assertNotIn("auto_relogin", json.loads(output.getvalue())["checks"])

    def test_switch_must_be_boolean(self):
        self.config.write_text(self.config.read_text().replace("auto_relogin=true", 'auto_relogin="yes"'))
        with self.assertRaises(ConfigError):
            load_config(self.config)


if __name__ == "__main__":
    unittest.main()
