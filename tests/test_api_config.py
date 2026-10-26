from __future__ import annotations

from http.client import IncompleteRead
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from jjz.api import (ApiClient, ApiError, BUSINESS_LOGIN_ORIGIN, MAX_RESPONSE_BYTES, ROUTE_PATH,
                     SUBMIT_PATH, _NoRedirect)
from jjz.config import ConfigError, Settings, load_config


def valid_application():
    return {
        "jjmd": "05,06", "jjmdmc": "公务差旅,其它", "area": "顺义区", "jjdq": "010",
        "xxdz": "测试地址", "jjdzgdjd": "116.6", "jjdzgdwd": "40.1", "sfzj": "1",
        "zjxxdz": "测试在京地址", "zjxxdzgdjd": "116.6", "zjxxdzgdwd": "40.3",
        "jjlk": "", "jjlkmc": "", "jjlkgdjd": "", "jjlkgdwd": "",
        "sqdzgdjd": "116.6", "sqdzgdwd": "40.3", "txrxx": [],
    }


def road_application():
    application = valid_application()
    application.update({"sfzj": "0", "zjxxdz": "", "zjxxdzgdjd": "", "zjxxdzgdwd": "",
                        "jjlk": "00306", "jjlkmc": "京沪高速（应寺检查站）",
                        "jjlkgdjd": "116.9", "jjlkgdwd": "39.6"})
    return application


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write_config(self, body):
        target = self.root / "config.toml"
        target.write_text(body, encoding="utf-8")
        return target

    def settings(self):
        path = self.write_config('[vehicle]\nplate="冀A00000"\n')
        return load_config(path)

    def write_login(self, body, mode=0o600):
        target = self.root / "secrets/login.toml"
        target.parent.mkdir(exist_ok=True)
        target.write_text(body, encoding="utf-8")
        target.chmod(mode)
        return target

    def test_login_section_is_rejected_in_main_config(self):
        path = self.write_config('[login]\nphone="13800000000"\npassword="SYNTHETIC_PASSWORD"\n')
        with self.assertRaises(ConfigError) as error:
            load_config(path)
        self.assertIn("secrets/login.toml", str(error.exception))
        self.assertNotIn("SYNTHETIC_PASSWORD", str(error.exception))

    def test_login_file_is_read_only_on_request_and_errors_hide_values(self):
        settings = self.settings()
        self.assertEqual(settings.login_file, self.root / "secrets/login.toml")
        self.assertEqual(settings.read_login(), ("", ""))
        self.write_login('[login]\nphone=" 13800000000 "\npassword="SYNTHETIC_PASSWORD"\n')
        self.assertEqual(settings.read_login(), ("13800000000", "SYNTHETIC_PASSWORD"))
        self.assertNotIn("SYNTHETIC_PASSWORD", repr(settings))
        for body, mode in (('[login]\npassword="SYNTHETIC_PASSWORD\\nNEXT"\n', 0o600),
                           ('[login]\npassword="SYNTHETIC_PASSWORD"\nextra="x"\n', 0o600),
                           ('[api]\nbase_url="SYNTHETIC_PASSWORD"\n', 0o600),
                           ('[login]\npassword="SYNTHETIC_PASSWORD"\n', 0o644),
                           ('[login]\npassword="SYNTHETIC_PASSWORD\n', 0o600)):
            with self.subTest(body=body, mode=mode):
                self.write_login(body, mode)
                with self.assertRaises(ConfigError) as error:
                    settings.read_login()
                self.assertNotIn("SYNTHETIC_PASSWORD", str(error.exception))

    def test_offline_config_allows_empty_service_and_resolves_paths(self):
        settings = self.settings()
        self.assertEqual(settings.base_url, "")
        self.assertEqual(settings.default_permit, "outside")
        self.assertEqual(settings.auth_file, self.root / "secrets/authorization.txt")
        self.assertEqual(settings.data_dir, self.root / "data")
        with self.assertRaises(ConfigError):
            settings.validate_live()

    def test_absolute_and_home_paths_remain_absolute(self):
        path = self.write_config('[api]\nauth_file="~/jjz-auth"\n[storage]\ndata_dir="/tmp/jjz-data"\n')
        settings = load_config(path)
        self.assertEqual(settings.auth_file, (Path.home() / "jjz-auth").resolve())
        self.assertEqual(settings.data_dir, Path("/tmp/jjz-data").resolve())

    def test_invalid_origins(self):
        for value in ("http://example.com", "https://user:password@example.com",
                      "https://@example.com",
                      "https://example.com/path", "https://example.com?token=x",
                      "https://example.com#token", "https://example.com:90000",
                      "https://example.com\\evil", "https://example.com hi"):
            with self.subTest(value=value):
                path = self.write_config('[api]\nbase_url=' + json.dumps(value) + '\n')
                with self.assertRaises(ConfigError) as error:
                    load_config(path)
                self.assertNotIn(value, str(error.exception))

    def test_valid_https_origin_normalizes_slash(self):
        path = self.write_config('[api]\nbase_url="https://example.com:443/"\n')
        self.assertEqual(load_config(path).base_url, "https://example.com:443")

    def test_types_ranges_and_time_are_checked(self):
        invalid = (
            '[api]\ntimeout_seconds=true\n', '[api]\ntimeout_seconds=0\n',
            '[api]\ntimeout_seconds=121\n', '[vehicle]\ndefault_permit="both"\n',
            '[renewal]\ne arliest_time="08:00"\n', '[renewal]\nearliest_time="8:00"\n',
            '[renewal]\nearliest_time="24:00"\n', '[renewal]\noutside_lead_days=true\n',
            '[renewal]\noutside_lead_days=2\n', '[renewal]\nsubmission_enabled=1\n',
            'api="not-a-table"\n', '[api]\nauth_file=""\n',
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(ConfigError):
                    load_config(self.write_config(body))

    def test_file_read_errors_and_invalid_toml_are_sanitized(self):
        for target in (self.root / "missing", self.write_config('secret="unfinished')):
            with self.assertRaises(ConfigError) as error:
                load_config(target)
            self.assertNotIn("unfinished", str(error.exception))

    def test_auth_permissions_and_single_line(self):
        settings = self.settings()
        settings.auth_file.parent.mkdir()
        settings.auth_file.write_text("secret-token", encoding="utf-8")
        settings.auth_file.chmod(0o644)
        with self.assertRaises(ConfigError):
            settings.read_authorization()
        settings.auth_file.chmod(0o600)
        self.assertEqual(settings.read_authorization(), "secret-token")
        for value in ("", "\n", "Bearer good\r\nInjected: leak", "secret\tvalue", "密钥"):
            settings.auth_file.write_text(value, encoding="utf-8")
            with self.subTest(value=value):
                with self.assertRaises(ConfigError) as error:
                    settings.read_authorization()
                if value.strip():
                    self.assertNotIn(value, str(error.exception))
        settings.auth_file.write_text("Bearer example\n", encoding="utf-8")
        self.assertEqual(settings.read_authorization(), "Bearer example")

    def test_auth_missing_directory_and_oversized_file_are_sanitized(self):
        settings = self.settings()
        with self.assertRaises(ConfigError):
            settings.read_authorization()
        settings.auth_file.parent.mkdir()
        settings.auth_file.mkdir(mode=0o700)
        with self.assertRaises(ConfigError):
            settings.read_authorization()
        settings.auth_file.rmdir()
        settings.auth_file.write_bytes(b"sensitive" * 2048)
        settings.auth_file.chmod(0o600)
        with self.assertRaises(ConfigError) as error:
            settings.read_authorization()
        self.assertNotIn("sensitive", str(error.exception))

    def test_application_requires_real_nonempty_fields_and_no_overrides(self):
        settings = self.settings()
        with self.assertRaises(ConfigError):
            settings.validate_application()
        settings.application = valid_application()
        settings.validate_application()
        for key, value in (("xxdz", ""), ("area", " "), ("sfzj", "2"), ("txrxx", {}),
                           ("sqdzgdjd", "181"), ("sqdzgdwd", "NaN"),
                           ("jjdzgdwd", "-91"), ("zjxxdzgdjd", "infinity"),
                           ("jjdq", "10"), ("jjmd", "5"), ("jjmd", "05,06,01"),
                           ("jjlk", "00306"), ("sqdzbdjd", "116.2"),
                           ("applyIdOld", "should-not-be-here"), ("vId", "server-owned")):
            with self.subTest(key=key, value=value):
                settings.application = valid_application()
                settings.application[key] = value
                with self.assertRaises(ConfigError):
                    settings.validate_application()

    def test_sfzj_selects_in_beijing_address_or_entry_road(self):
        settings = self.settings()
        settings.application = road_application()
        settings.validate_application()
        for key in ("jjlk", "jjlkmc", "jjlkgdjd", "jjlkgdwd"):
            with self.subTest(key=key):
                settings.application = road_application()
                settings.application[key] = ""
                with self.assertRaises(ConfigError):
                    settings.validate_application()
        settings.application = road_application()
        settings.application["zjxxdz"] = "不应填写"
        with self.assertRaises(ConfigError):
            settings.validate_application()
        settings.application = valid_application()
        for key in ("jjlk", "jjlkmc", "jjlkgdjd", "jjlkgdwd"):
            del settings.application[key]
        settings.validate_application()

    def test_inside_application_has_no_implicit_outside_fallback(self):
        settings = self.settings()
        settings.application = valid_application()
        settings.validate_application("outside")
        self.assertEqual(settings.application_for("inside"), {})
        with self.assertRaisesRegex(ConfigError, "inside_application"):
            settings.validate_application("inside")
        for method in (settings.application_for, settings.validate_application):
            with self.assertRaises(ConfigError):
                method("both")

    def test_inside_application_uses_its_own_complete_template(self):
        settings = self.settings()
        settings.application = valid_application()
        settings.inside_application = valid_application()
        settings.inside_application["xxdz"] = "另一目的地址"
        settings.inside_application["zjxxdz"] = "另一在京地址"
        settings.inside_application["sqdzgdjd"] = "117.0"
        settings.validate_application("inside")
        self.assertEqual(settings.application_for()["xxdz"], "测试地址")
        self.assertEqual(settings.application_for("inside")["xxdz"], "另一目的地址")
        self.assertEqual(settings.application_for("inside")["sqdzgdjd"], "117.0")

    def test_inside_application_loads_as_optional_separate_table(self):
        path = self.write_config('[application]\nxxdz="外证地址"\n[inside_application]\nxxdz="内证地址"\n')
        settings = load_config(path)
        self.assertEqual(settings.application_for("outside"), {"xxdz": "外证地址"})
        self.assertEqual(settings.application_for("inside"), {"xxdz": "内证地址"})
        with self.assertRaises(ConfigError):
            settings.validate_application("inside")


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = Mock(spec=Settings)
        self.settings.base_url = "https://jjz.jtgl.beijing.gov.cn"
        self.settings.timeout_seconds = 17
        self.settings.read_authorization.return_value = "private-test-token"
        self.client = ApiClient(self.settings)
        self.client.opener = Mock()

    def response(self, body):
        response = Mock()
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.client.opener.open.return_value = response
        return response

    def test_state_uses_post_with_header_and_bounded_read(self):
        response = self.response(b'{"code":200,"data":{}}')
        self.assertEqual(self.client.state(), {"code": 200, "data": {}})
        req = self.client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://jjz.jtgl.beijing.gov.cn/pro/applyRecordController/stateList")
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.get_header("Authorization"), "private-test-token")
        self.assertEqual(json.loads(req.data), {})
        self.assertEqual(self.client.opener.open.call_args.kwargs["timeout"], 17)
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)

    def test_credential_is_sent_only_to_official_service_origins(self):
        for origin in ("https://example.invalid", "https://jjz.jtgl.beijing.gov.cn:443",
                       "https://jjz.jtgl.beijing.gov.cn.example.invalid"):
            with self.subTest(origin=origin):
                self.settings.base_url = origin
                for method in (self.client.state, lambda: self.client.submit({})):
                    with self.assertRaises(ConfigError):
                        method()
        self.client.opener.open.assert_not_called()
        self.settings.read_authorization.assert_not_called()
        self.response(b'{"code":200,"data":{}}')
        for origin in ("https://jjz.jtgl.beijing.gov.cn", BUSINESS_LOGIN_ORIGIN):
            with self.subTest(origin=origin):
                self.settings.base_url = origin
                self.client.state()
                self.assertEqual(self.client.opener.open.call_args.args[0].full_url,
                                 origin + "/pro/applyRecordController/stateList")

    def test_check_only_reaches_known_read_only_endpoints(self):
        self.response(b'{"code":200,"data":"0"}')
        self.assertEqual(self.client.check(ROUTE_PATH, {"vId": "v"})["data"], "0")
        req = self.client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, "https://jjz.jtgl.beijing.gov.cn" + ROUTE_PATH)
        self.client.opener.open.reset_mock()
        for path in (SUBMIT_PATH, "/pro/applyRecordController/other"):
            with self.subTest(path=path), self.assertRaises(ApiError):
                self.client.check(path, {})
        self.client.opener.open.assert_not_called()

    def test_submit_returns_business_ack_without_claiming_approval(self):
        self.response(b'{"code":"200","data":null}')
        result = self.client.submit({"jjrq": "2026-10-26"})
        req = self.client.opener.open.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/insertApplyRecord"))
        self.assertEqual(result["data"], None)

    def test_submit_transport_errors_are_uncertain_and_state_errors_are_not(self):
        for error in (TimeoutError("private-test-token"), URLError("sensitive-host"),
                      IncompleteRead(b"sensitive-body")):
            for method in (self.client.state, lambda: self.client.submit({})):
                self.client.opener.open.side_effect = error
                with self.assertRaises(ApiError) as context:
                    method()
                self.assertEqual(context.exception.uncertain, method != self.client.state)
                self.assertNotIn("private-test-token", str(context.exception))
                self.assertNotIn("sensitive", str(context.exception))

    def test_response_read_failure_after_open_is_uncertain(self):
        response = self.response(b"")
        response.read.side_effect = TimeoutError("sensitive-content")
        with self.assertRaises(ApiError) as error:
            self.client.submit({})
        self.assertTrue(error.exception.uncertain)
        self.assertNotIn("sensitive", str(error.exception))

    def test_invalid_payload_is_definite_before_network(self):
        with self.assertRaises(ApiError) as error:
            self.client.submit({"unsupported": object()})
        self.assertFalse(error.exception.uncertain)
        self.client.opener.open.assert_not_called()

    def test_http_auth_failure_is_distinct(self):
        for status in (401, 403):
            self.client.opener.open.side_effect = HTTPError(
                "https://sensitive-host", status, "secret-message", {}, io.BytesIO(b"secret-body"))
            with self.assertRaises(ApiError) as error:
                self.client.submit({})
            self.assertTrue(error.exception.auth_expired)
            self.assertFalse(error.exception.uncertain)
            self.assertNotIn("secret", str(error.exception))

    def test_redirects_are_not_followed_and_submit_is_uncertain(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.invalid"))
        self.client.opener.open.side_effect = HTTPError("", 302, "", {}, None)
        with self.assertRaises(ApiError) as error:
            self.client.submit({})
        self.assertTrue(error.exception.uncertain)

    def test_http_5xx_is_uncertain(self):
        self.client.opener.open.side_effect = HTTPError("", 503, "", {}, None)
        with self.assertRaises(ApiError) as error:
            self.client.submit({})
        self.assertTrue(error.exception.uncertain)

    def test_business_rejection_is_definite_and_sanitized(self):
        self.response(b'{"code":500,"msg":"sensitive personal data"}')
        with self.assertRaises(ApiError) as error:
            self.client.submit({})
        self.assertFalse(error.exception.uncertain)
        self.assertNotIn("sensitive", str(error.exception))

    def test_bad_response_is_uncertain_for_mutations_only(self):
        for body in (b"not-json", b"[]", b'{}', b'{"code":true}', b'\xff'):
            for mutation in (False, True):
                with self.subTest(body=body, mutation=mutation):
                    self.response(body)
                    with self.assertRaises(ApiError) as error:
                        self.client.submit({}) if mutation else self.client.state()
                    self.assertEqual(error.exception.uncertain, mutation)

    def test_auth_failure_inside_business_envelope(self):
        self.response(b'{"code":"401","msg":"secret"}')
        with self.assertRaises(ApiError) as error:
            self.client.state()
        self.assertTrue(error.exception.auth_expired)

    def test_oversized_response_is_rejected(self):
        self.response(b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(ApiError) as error:
            self.client.submit({})
        self.assertTrue(error.exception.uncertain)


if __name__ == "__main__":
    unittest.main()
