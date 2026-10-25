from __future__ import annotations

import base64
from hashlib import md5
from http.client import IncompleteRead
import io
import json
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request

from jjz.auth import (
    API_ROOT, CALLBACK_URL, ENTRY_URL, MAX_RESPONSE_BYTES, PORTAL_ORIGIN,
    AuthError, LoginGateway, _OfficialRedirect, _encrypt, official_url,
)

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
except ImportError:
    serialization = padding = rsa = None


class Response(io.BytesIO):
    def __init__(self, body=b"", url=CALLBACK_URL):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def envelope(data=None, code=0, **extra):
    return json.dumps({"meta": {"code": code, **extra}, "data": data}).encode()


class UrlTests(unittest.TestCase):
    def test_rejects_nonofficial_or_malformed_destinations(self):
        values = ("http://bjt.beijing.gov.cn/", "https://example.com/",
                  "https://bjt.beijing.gov.cn.evil.test/", "https://user@bjt.beijing.gov.cn/",
                  "https://bjt.beijing.gov.cn:444/", "https://bjt.beijing.gov.cn\\evil/",
                  "https://bjt.beijing.gov.cn/\n", None, [], "")
        for value in values:
            with self.subTest(value=value), self.assertRaises(AuthError):
                official_url(value)
        self.assertEqual(official_url(CALLBACK_URL), CALLBACK_URL)

    def test_redirects_validate_before_forwarding(self):
        handler = _OfficialRedirect()
        post = Request(API_ROOT + "inner/m/login/doUserLoginByPwd", b"secret-body",
                       {"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        for code in (302, 303, 307, 308):
            with self.subTest(code=code), self.assertRaises(AuthError):
                handler.redirect_request(post, None, code, "redirect", {}, "https://example.com/")
        for code in (307, 308):
            with self.subTest(code=code), self.assertRaises(AuthError):
                handler.redirect_request(post, None, code, "redirect", {}, CALLBACK_URL)
        for code in (302, 303):
            redirected = handler.redirect_request(post, None, code, "redirect", {}, CALLBACK_URL)
            self.assertEqual(redirected.get_method(), "GET")
            self.assertIsNone(redirected.data)
            self.assertFalse(redirected.has_header("Content-Type"))


@unittest.skipIf(rsa is None, "cryptography optional login dependency unavailable")
class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        cls.public_key = base64.b64encode(cls.key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
        )).decode()

    def ready(self, *responses):
        gateway = LoginGateway()
        # Preserve raw '+' exactly as the live official redirect does.
        login_url = PORTAL_ORIGIN + "/m/index.html?pubKey=" + self.public_key
        gateway.opener = FakeOpener(Response(b"public html", login_url), Response(), *responses)
        self.assertEqual(gateway.preflight(), {
            "entry_reachable": True, "callback_reachable": True, "certificate_valid": True,
        })
        return gateway

    def decrypt_fields(self, request):
        fields = parse_qs(request.data.decode())
        encrypted = base64.b64decode(fields["encryptData"][0])
        size = self.key.key_size // 8
        plaintext = b"".join(self.key.decrypt(encrypted[i:i + size], padding.PKCS1v15())
                             for i in range(0, len(encrypted), size))
        return fields, json.loads(plaintext)

    def test_preflight_uses_same_cookie_opener_and_raw_base64_key(self):
        gateway = self.ready()
        self.assertEqual(gateway._public_key, self.public_key)
        self.assertEqual([call[0].full_url for call in gateway.opener.calls], [ENTRY_URL, CALLBACK_URL])
        self.assertTrue(all(call[1] == 20 for call in gateway.opener.calls))

    def test_preflight_allows_callback_missing_auth_code_http_400(self):
        gateway = LoginGateway()
        callback = HTTPError(CALLBACK_URL, 400, "Missing auth code", {}, io.BytesIO(b"missing code"))
        gateway.opener = FakeOpener(
            Response(b"public html", PORTAL_ORIGIN + "/m/index.html?" + urlencode({"pubKey": self.public_key})),
            callback,
        )
        self.assertTrue(gateway.preflight()["certificate_valid"])

    def test_certificate_failure_blocks_later_credential_transmission(self):
        gateway = LoginGateway()
        gateway.opener = FakeOpener(
            Response(b"html", PORTAL_ORIGIN + "/m/index.html?pubKey=" + self.public_key),
            URLError(ssl.SSLCertVerificationError("SYNTHETIC_RAW_CERT_ERROR")),
        )
        with self.assertRaises(AuthError) as error:
            gateway.preflight()
        self.assertEqual(error.exception.category, "certificate")
        self.assertNotIn("SYNTHETIC_RAW_CERT_ERROR", str(error.exception))
        for method, arguments in ((gateway.captcha, ()), (gateway.send_sms, ("13800000000", "abcd")),
                                  (gateway.login_password, ("13800000000", "secret", "abcd")),
                                  (gateway.login_sms, ("13800000000", "123456"))):
            with self.subTest(method=method), self.assertRaises(AuthError) as error:
                method(*arguments)
            self.assertEqual(error.exception.category, "certificate")
        self.assertEqual(len(gateway.opener.calls), 2)

    def test_new_preflight_does_not_reuse_failed_or_old_key(self):
        gateway = self.ready()
        gateway.opener = FakeOpener(URLError("SYNTHETIC_NETWORK_MESSAGE"))
        with self.assertRaises(AuthError) as error:
            gateway.preflight()
        self.assertEqual(error.exception.category, "network")
        self.assertIsNone(gateway._public_key)
        with self.assertRaises(AuthError):
            gateway.send_sms("13800000000", "abcd")
        self.assertEqual(len(gateway.opener.calls), 1)

    def test_preflight_rejects_missing_duplicate_or_wrong_landing_key(self):
        for url in (PORTAL_ORIGIN + "/m/index.html", PORTAL_ORIGIN + "/m/index.html?pubKey=",
                    PORTAL_ORIGIN + "/m/index.html?pubKey=A&pubKey=B", CALLBACK_URL):
            with self.subTest(url=url):
                gateway = LoginGateway()
                gateway.opener = FakeOpener(Response(url=url))
                with self.assertRaises(AuthError):
                    gateway.preflight()
                self.assertEqual(len(gateway.opener.calls), 1)

    def test_password_protocol_encrypts_expected_fields_without_plaintext(self):
        result = {"certLevel": 3, "grantLevel": 2, "redirectUrl": CALLBACK_URL + "?code=private-ticket"}
        gateway = self.ready(Response(envelope(result), API_ROOT + "inner/m/login/doUserLoginByPwd"))
        logged_in = gateway.login_password("13800000000", "SYNTHETIC_PASSWORD", "aB12")
        request = gateway.opener.calls[-1][0]
        fields, payload = self.decrypt_fields(request)
        self.assertEqual(payload, {"userIdentity": "13800000000",
                                   "encryptedPwd": md5(b"SYNTHETIC_PASSWORD").hexdigest(), "resetFlag": False})
        self.assertEqual(fields["captcha"], ["aB12"])
        self.assertNotIn(b"SYNTHETIC_PASSWORD", request.data)
        self.assertNotIn(b"13800000000", request.data)
        self.assertEqual(request.get_header("Origin"), PORTAL_ORIGIN)
        self.assertEqual(request.get_header("X-requested-with"), "XMLHttpRequest")
        self.assertEqual(logged_in, {"redirect_url": result["redirectUrl"], "cert_level": 3})
        self.assertNotIn("authorization", logged_in)

    def test_send_sms_fields_and_sms_login_are_distinct(self):
        data = {"certLevel": "3", "grantLevel": "3", "redirectUrl": CALLBACK_URL}
        gateway = self.ready(Response(envelope()), Response(envelope(data)))
        gateway.send_sms("13800000000", "aB12")
        sent = parse_qs(gateway.opener.calls[-1][0].data.decode())
        self.assertEqual(sent, {"mobile": ["13800000000"], "captcha": ["aB12"],
                                "type": ["login"], "smsType": ["login_type"]})
        logged_in = gateway.login_sms("13800000000", "123456")
        fields, payload = self.decrypt_fields(gateway.opener.calls[-1][0])
        self.assertEqual(set(fields), {"encryptData"})
        self.assertEqual(payload, {"mobile": "13800000000", "smsCode": "123456"})
        self.assertEqual(logged_in["cert_level"], 3)

    def test_captcha_requires_known_image_signature(self):
        for signature in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a"):
            gateway = self.ready(Response(signature + b"synthetic image"))
            self.assertEqual(gateway.captcha(), signature + b"synthetic image")
            self.assertTrue(gateway.opener.calls[-1][0].full_url.startswith(API_ROOT + "common/generateCaptcha?"))
        gateway = self.ready(Response(b"<html>not a captcha</html>"))
        with self.assertRaises(AuthError):
            gateway.captcha()

    def test_login_requires_real_name_level_and_official_redirect(self):
        examples = (
            ({"certLevel": 1, "grantLevel": 3, "redirectUrl": CALLBACK_URL}, "verification"),
            ({"certLevel": None, "grantLevel": 3, "redirectUrl": CALLBACK_URL}, "protocol"),
            ({"certLevel": True, "grantLevel": 3, "redirectUrl": CALLBACK_URL}, "protocol"),
            ({"certLevel": 3, "grantLevel": 3, "redirectUrl": "https://evil.test/?token=secret"}, "protocol"),
            ({"certLevel": 3, "grantLevel": 3}, "protocol"),
        )
        for data, category in examples:
            with self.subTest(data=data):
                gateway = self.ready(Response(envelope(data)))
                with self.assertRaises(AuthError) as error:
                    gateway.login_sms("13800000000", "123456")
                self.assertEqual(error.exception.category, category)

    def test_failed_login_is_fixed_and_sanitized(self):
        for code, category in ((5016, "verification"), ("5017", "verification"),
                               (5020, "credentials"), (6109, "credentials"), (5031, "credentials")):
            with self.subTest(code=code):
                gateway = self.ready(Response(envelope(code=code, message="SYNTHETIC_PHONE_PASSWORD_TOKEN")))
                with self.assertRaises(AuthError) as error:
                    gateway.login_sms("13800000000", "123456")
                self.assertEqual(error.exception.category, category)
                self.assertNotIn("SYNTHETIC_PHONE_PASSWORD_TOKEN", str(error.exception))

    def test_official_letter_levels_and_sublevels_preserve_official_order(self):
        for cert, grant in (("L3", "L2"), ("L31", "L3"), ("L33", "L32"), ("L2", "L2")):
            with self.subTest(cert=cert, grant=grant):
                data = {"certLevel": cert, "grantLevel": grant, "redirectUrl": CALLBACK_URL}
                gateway = self.ready(Response(envelope(data)))
                self.assertEqual(gateway.login_sms("13800000000", "123456")["cert_level"], cert)
        gateway = self.ready(Response(envelope({"certLevel": "L31", "grantLevel": "L32",
                                                "redirectUrl": CALLBACK_URL})))
        with self.assertRaises(AuthError) as error:
            gateway.login_sms("13800000000", "123456")
        self.assertEqual(error.exception.category, "verification")

    def test_unknown_or_mixed_certification_formats_cannot_claim_success(self):
        for cert, grant in (("L4", "L3"), ("L3", 2), (3, "L2"), (99, 3), ("L00", "L1")):
            with self.subTest(cert=cert, grant=grant):
                data = {"certLevel": cert, "grantLevel": grant, "redirectUrl": CALLBACK_URL}
                gateway = self.ready(Response(envelope(data)))
                with self.assertRaises(AuthError) as error:
                    gateway.login_sms("13800000000", "123456")
                self.assertEqual(error.exception.category, "protocol")

    def test_malformed_json_envelopes_cannot_claim_success(self):
        for value in (b"not-json", b"[]", b"{}", b'{"meta":null}', b'{"meta":{"code":false}}',
                      b'{"meta":{"code":0},"data":null}'):
            with self.subTest(value=value):
                gateway = self.ready(Response(value))
                with self.assertRaises(AuthError) as error:
                    gateway.login_sms("13800000000", "123456")
                self.assertEqual(error.exception.category, "protocol")

    def test_follow_callback_does_not_extract_or_promote_token(self):
        landing = "https://bjjj.jtgl.beijing.gov.cn/uc/ucfront/userauth?token=unverified-auth-token"
        gateway = self.ready(Response(b"<html>official business page</html>", landing))
        result = gateway.follow_callback({"redirect_url": CALLBACK_URL + "?code=private-ticket"})
        self.assertEqual(result, {"final_url": landing})
        self.assertNotIn("authorization", result)

    def test_invalid_inputs_never_send_requests(self):
        gateway = self.ready()
        for method, arguments in ((gateway.login_password, ("not-phone", "secret", "abc1")),
                                  (gateway.login_password, ("13800000000", "", "abc1")),
                                  (gateway.send_sms, ("13800000000", "bad\nchallenge")),
                                  (gateway.login_sms, ("13800000000", "abc1"))):
            with self.subTest(method=method), self.assertRaises(AuthError):
                method(*arguments)
        self.assertEqual(len(gateway.opener.calls), 2)

    def test_oversized_response_and_interrupted_read_are_safe(self):
        gateway = self.ready(Response(b"x" * (MAX_RESPONSE_BYTES + 1)))
        with self.assertRaises(AuthError):
            gateway.captcha()
        response = Response()
        response.read = Mock(side_effect=IncompleteRead(b"private response", 5))
        gateway = self.ready(response)
        with self.assertRaises(AuthError) as error:
            gateway.captcha()
        self.assertEqual(error.exception.category, "network")
        self.assertNotIn("private response", str(error.exception))

    def test_long_unicode_rsa_is_concatenated_blocks(self):
        value = {"text": "多字节内容" * 100}
        ciphertext = base64.b64decode(_encrypt(self.public_key, value))
        size = self.key.key_size // 8
        recovered = b"".join(self.key.decrypt(ciphertext[i:i + size], padding.PKCS1v15())
                             for i in range(0, len(ciphertext), size))
        self.assertEqual(json.loads(recovered), value)

    def test_invalid_public_key_is_sanitized_and_never_sent(self):
        gateway = self.ready()
        gateway._public_key = "SYNTHETIC_INVALID_KEY" * 5
        with self.assertRaises(AuthError) as error:
            gateway.login_sms("13800000000", "123456")
        self.assertNotIn("SYNTHETIC_INVALID_KEY", str(error.exception))
        self.assertEqual(len(gateway.opener.calls), 2)


if __name__ == "__main__":
    unittest.main()
