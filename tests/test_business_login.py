"""Business-login boundaries using synthetic tickets and local mock responses."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from jjz.api import (
    ApiClient, ApiError, BUSINESS_LOGIN_ORIGIN, BUSINESS_LOGIN_PATHS,
    BusinessLoginClient, H5_SOURCE, LOGIN_SOURCES, MAX_RESPONSE_BYTES, STATE_PATH,
)
from jjz.auth import AuthError
from jjz.config import ConfigError, Settings
from jjz.credentials import callback_ticket, verify_phone_authorization
from jjz.domain import DomainError


TICKET = "SYNTHETIC_TICKET+PART/="
AUTHORIZATION = "SYNTHETIC_BUSINESS_AUTHORIZATION"
PLATE = "冀ATEST01"


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self, item):
        self.item = item
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.item, BaseException):
            raise self.item
        return self.item


def envelope(data=None, code=200, **extra):
    return json.dumps({"code": code, "data": data, **extra}).encode("utf-8")


class CallbackTicketTests(unittest.TestCase):
    def test_selects_only_the_exact_two_published_landing_paths(self):
        for path in BUSINESS_LOGIN_PATHS:
            with self.subTest(path=path):
                result = callback_ticket(BUSINESS_LOGIN_ORIGIN + path + "?token=SYNTHETIC&state=300001")
                self.assertEqual(result, (path, "SYNTHETIC", "300001"))

    def test_decode_uri_component_preserves_plus_and_decodes_only_once(self):
        path = "/login_h5_byUserCenter.html"
        url = BUSINESS_LOGIN_ORIGIN + path + "?token=SYNTHETIC+TICKET%2F%3D&state=300001"
        self.assertEqual(callback_ticket(url), (path, "SYNTHETIC+TICKET/=", "300001"))
        url = BUSINESS_LOGIN_ORIGIN + path + "?token=SYNTHETIC%252BTICKET&state=300001"
        self.assertEqual(callback_ticket(url)[1], "SYNTHETIC%2BTICKET")

    def test_missing_state_uses_the_published_page_default(self):
        result = callback_ticket(BUSINESS_LOGIN_ORIGIN + "/login_h5.html?token=SYNTHETIC")
        self.assertEqual(result[2], "101000004071")

    def test_duplicate_empty_or_malformed_query_parameters_stop(self):
        queries = (
            "state=300001", "token=", "token=SYNTHETIC&state=",
            "token=ONE&token=TWO", "token=ONE&%74oken=TWO",
            "token=SYNTHETIC&state=300001&state=300001",
            "token=SYNTHETIC%ZZ", "token=SYNTHETIC%", "token=%FF",
            "token=%20SYNTHETIC", "token=SYNTHETIC%0A", "token=SYNTHETIC%7F",
        )
        for query in queries:
            with self.subTest(query=query), self.assertRaises(AuthError):
                callback_ticket(BUSINESS_LOGIN_ORIGIN + "/login_h5.html?" + query)

    def test_foreign_origin_port_credentials_fragment_or_unknown_page_stop(self):
        suffix = "/login_h5.html?token=SYNTHETIC"
        values = (
            "https://ssp.jtgl.beijing.gov.cn/sso/callback/userauth?token=SYNTHETIC",
            "https://bjjj.jtgl.beijing.gov.cn" + suffix,
            "https://jjz.jtgl.beijing.gov.cn" + suffix,
            "https://jjz.jtgl.beijing.gov.cn:443" + suffix,
            "https://jjz.jtgl.beijing.gov.cn:2444" + suffix,
            "http://jjz.jtgl.beijing.gov.cn:2443" + suffix,
            "https://jjz.jtgl.beijing.gov.cn.evil.invalid:2443" + suffix,
            "https://synthetic@jjz.jtgl.beijing.gov.cn:2443" + suffix,
            BUSINESS_LOGIN_ORIGIN + "/unknown.html?token=SYNTHETIC",
            BUSINESS_LOGIN_ORIGIN + suffix + "#fragment",
            "\n" + BUSINESS_LOGIN_ORIGIN + suffix,
            None,
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(AuthError) as raised:
                callback_ticket(value)
            self.assertNotIn("SYNTHETIC", str(raised.exception))


class BusinessLoginTests(unittest.TestCase):
    def client(self, body=None, error=None):
        client = BusinessLoginClient(timeout=17)
        client.opener = Opener(error if error is not None else Response(
            body if body is not None else envelope({"accessToken": AUTHORIZATION})))
        return client

    def test_each_landing_selects_its_own_fixed_official_exchange_endpoint(self):
        for path, endpoint in BUSINESS_LOGIN_PATHS.items():
            with self.subTest(path=path):
                client = self.client()
                result = client.exchange(path, TICKET, "300001")
                self.assertEqual(result, AUTHORIZATION)
                request, timeout = client.opener.calls[0]
                url = urlsplit(request.full_url)
                self.assertEqual(url.scheme + "://" + url.netloc, BUSINESS_LOGIN_ORIGIN)
                self.assertEqual(url.path, endpoint)
                self.assertEqual(parse_qs(url.query), {"state": ["300001"]})
                self.assertNotIn(TICKET, request.full_url)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(json.loads(request.data), {"token": TICKET, "state": "300001"})
                self.assertEqual(request.get_header("Content-type"), "application/json")
                self.assertEqual(request.get_header("Source"), H5_SOURCE)
                self.assertEqual(request.get_header("Origin"), BUSINESS_LOGIN_ORIGIN)
                self.assertEqual(request.get_header("Referer"), BUSINESS_LOGIN_ORIGIN + path)
                self.assertEqual(timeout, 17)

    def test_known_channel_state_is_used_as_source(self):
        state = next(value for value in LOGIN_SOURCES if value != H5_SOURCE)
        client = self.client()
        client.exchange("/login_h5.html", TICKET, state)
        self.assertEqual(client.opener.calls[0][0].get_header("Source"), state)

    def test_unknown_path_or_invalid_state_never_opens_a_connection(self):
        values = (
            ("/auth/userController/loginUser", "300001"),
            ("https://evil.invalid/login_h5.html", "300001"),
            ("/login_h5.html", ""), ("/login_h5.html", "x\r\nInjected: value"),
            ("/login_h5.html", "渠道"), ("/login_h5.html", None),
        )
        for path, state in values:
            with self.subTest(path=path, state=state):
                client = self.client()
                with self.assertRaises(ApiError):
                    client.exchange(path, TICKET, state)
                self.assertEqual(client.opener.calls, [])

    def test_only_access_token_is_promoted_and_invalid_tokens_stop(self):
        data_values = (
            {"token": AUTHORIZATION}, {}, None, [],
            {"accessToken": ""}, {"accessToken": "  "},
            {"accessToken": "SYNTHETIC\r\nInjected: value"},
            {"accessToken": "SYNTHETIC_非ASCII"}, {"accessToken": 123},
        )
        for data in data_values:
            with self.subTest(data=data):
                client = self.client(envelope(data))
                with self.assertRaises(ApiError) as raised:
                    client.exchange("/login_h5.html", TICKET, "300001")
                self.assertNotIn(AUTHORIZATION, str(raised.exception))
                self.assertNotIn(TICKET, str(raised.exception))

    def test_malformed_or_rejected_responses_never_claim_success(self):
        bodies = (
            b"not-json", b"[]", b"{}", b'{"code":true,"data":{}}',
            envelope({"accessToken": AUTHORIZATION}, code=500, msg="SYNTHETIC_SERVER_DETAIL"),
        )
        for body in bodies:
            with self.subTest(body=body):
                client = self.client(body)
                with self.assertRaises(ApiError) as raised:
                    client.exchange("/login_h5.html", TICKET, "300001")
                self.assertNotIn("SYNTHETIC_SERVER_DETAIL", str(raised.exception))
                self.assertNotIn(AUTHORIZATION, str(raised.exception))

    def test_http_auth_failures_and_network_errors_are_sanitized(self):
        for status in (302, 401, 403, 500):
            with self.subTest(status=status):
                error = HTTPError(BUSINESS_LOGIN_ORIGIN, status, "SYNTHETIC_RAW_DETAIL", {},
                                  io.BytesIO(b"SYNTHETIC_BODY"))
                client = self.client(error=error)
                with self.assertRaises(ApiError) as raised:
                    client.exchange("/login_h5.html", TICKET, "300001")
                self.assertEqual(raised.exception.auth_expired, status in (401, 403))
                self.assertNotIn("SYNTHETIC", str(raised.exception))
        for error in (URLError("SYNTHETIC_RAW_DETAIL"), TimeoutError("SYNTHETIC_RAW_DETAIL")):
            client = self.client(error=error)
            with self.assertRaises(ApiError) as raised:
                client.exchange("/login_h5.html", TICKET, "300001")
            self.assertNotIn("SYNTHETIC", str(raised.exception))

    def test_response_limit_and_string_success_code(self):
        client = self.client(b"x" * (MAX_RESPONSE_BYTES + 1))
        with self.assertRaises(ApiError):
            client.exchange("/login_h5.html", TICKET, "300001")
        client = self.client(envelope({"accessToken": AUTHORIZATION}, code="200"))
        self.assertEqual(client.exchange("/login_h5.html", TICKET, "300001"), AUTHORIZATION)


class MemoryVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = Settings(config_path=self.root / "synthetic-config.toml",
                                 base_url=BUSINESS_LOGIN_ORIGIN,
                                 auth_file=self.root / "missing-authorization", plate=PLATE)

    def test_memory_api_query_does_not_read_or_require_an_existing_credential(self):
        self.settings.read_authorization = Mock(side_effect=AssertionError("must not read old credential"))
        self.settings.validate_live = Mock(side_effect=AssertionError("must not validate old credential"))
        client = ApiClient(self.settings, authorization=AUTHORIZATION)
        client.opener = Opener(Response(envelope({"bzclxx": []})))
        self.assertEqual(client.state()["code"], 200)
        request, _ = client.opener.calls[0]
        self.assertEqual(request.full_url, BUSINESS_LOGIN_ORIGIN + STATE_PATH)
        self.assertEqual(request.get_header("Authorization"), AUTHORIZATION)
        self.settings.read_authorization.assert_not_called()
        self.settings.validate_live.assert_not_called()
        self.assertFalse(self.settings.auth_file.exists())

    def test_exact_target_vehicle_is_required_before_success_without_disk_writes(self):
        client = Mock()
        client.state.return_value = {"code": 200, "data": {"bzclxx": [
            {"hphm": PLATE, "ecbzxx": None, "bzxx": None},
        ]}}
        factory = Mock(return_value=client)
        self.settings.read_authorization = Mock(side_effect=AssertionError("must not read old credential"))
        self.assertEqual(verify_phone_authorization(self.settings, AUTHORIZATION, factory), 0)
        self.assertEqual(factory.call_args.kwargs, {"authorization": AUTHORIZATION})
        candidate = factory.call_args.args[0]
        self.assertEqual(candidate.base_url, BUSINESS_LOGIN_ORIGIN)
        client.state.assert_called_once_with()
        client.submit.assert_not_called()
        self.settings.read_authorization.assert_not_called()
        self.assertFalse(self.settings.auth_file.exists())
        self.assertEqual(list(self.root.iterdir()), [])

        client.state.return_value = {"code": 200, "data": {"bzclxx": [{"hphm": "SYNTHETIC_OTHER"}]}}
        with self.assertRaises(DomainError):
            verify_phone_authorization(self.settings, AUTHORIZATION, factory)
        self.assertFalse(self.settings.auth_file.exists())

    def test_unknown_service_or_demo_vehicle_stops_before_any_query(self):
        factory = Mock()
        for origin, plate in (("https://foreign.invalid", PLATE), (BUSINESS_LOGIN_ORIGIN, "冀A00000")):
            with self.subTest(origin=origin, plate=plate):
                self.settings.base_url, self.settings.plate = origin, plate
                with self.assertRaises(ConfigError):
                    verify_phone_authorization(self.settings, AUTHORIZATION, factory)
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
