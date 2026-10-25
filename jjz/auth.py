"""Interactive unified-authentication client, based on the official H5 flow.

This module never treats an authentication redirect token as a permit-service
Authorization. Credentials, challenges and cookies remain in memory only.
"""

from __future__ import annotations

import base64
import hashlib
from http.client import HTTPException
from http.cookiejar import CookieJar
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener

from .api import (
    AUTH_ALLOWED_ORIGINS as ALLOWED_ORIGINS,
    AUTH_CAPTCHA_PATH,
    AUTH_PASSWORD_LOGIN_PATH,
    AUTH_SEND_SMS_PATH,
    AUTH_SMS_LOGIN_PATH,
    BUSINESS_LOGIN_PAGE as BUSINESS_ENTRY_URL,
    MAX_RESPONSE_BYTES,
    UNIFIED_AUTH_API_ROOT as API_ROOT,
    UNIFIED_AUTH_CALLBACK_URL as CALLBACK_URL,
    UNIFIED_AUTH_ENTRY_URL as ENTRY_URL,
    UNIFIED_AUTH_PORTAL_ORIGIN as PORTAL_ORIGIN,
)


class AuthError(RuntimeError):
    """Only fixed, non-sensitive messages may be exposed to a CLI/log."""

    def __init__(self, message: str, *, category: str = "protocol"):
        super().__init__(message)
        self.category = category


def official_url(value: str) -> str:
    """Reject unfamiliar destinations before any request or redirect is sent."""
    try:
        if not isinstance(value, str) or not value or len(value) > 32768:
            raise ValueError
        if any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or (parsed.hostname, parsed.port or 443) not in ALLOWED_ORIGINS
                or parsed.username is not None or parsed.password is not None
                or parsed.port == 0):
            raise ValueError
        parsed.netloc.encode("ascii")
    except (ValueError, UnicodeError):
        raise AuthError("登录流程返回了未支持的跳转地址，已停止。") from None
    return value


class _OfficialRedirect(HTTPRedirectHandler):
    """Never forward a password/SMS POST body to a redirect destination."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        official_url(newurl)
        if req.get_method() == "POST" and code not in (302, 303):
            raise AuthError("登录请求发生了未支持的重定向，已停止。")
        # urllib turns 302/303 POST requests into GET and drops entity headers.
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if redirected is not None and (old.hostname, old.port or 443) != (new.hostname, new.port or 443):
            for name in ("Authorization", "Cookie", "Origin", "Referer", "Proxy-authorization"):
                redirected.remove_header(name)
        return redirected


def _certificate_error(error: BaseException) -> bool:
    current: object = error
    for _ in range(4):
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = getattr(current, "reason", None)
        if current is None:
            return False
    return False


def _phone(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"1[3-9][0-9]{9}", value):
        raise AuthError("请填写有效的中国大陆手机号。", category="credentials")
    return value


def _challenge(value: str, *, sms=False) -> str:
    pattern = r"[0-9]{4,8}" if sms else r"[A-Za-z0-9]{1,16}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise AuthError("验证码格式不符合预期，请在本机重新输入。", category="verification")
    return value


def _encrypt(public_key: str, value: dict) -> str:
    """Use the official protocol's RSA PKCS#1 v1.5 Unicode-long encryption."""
    executable = shutil.which("openssl")
    if executable is None:
        raise AuthError("手机号登录需要本机 OpenSSL 命令，请先检查 openssl 是否可用。", category="dependency")
    try:
        if not isinstance(public_key, str) or not 64 <= len(public_key) <= 16384:
            raise ValueError
        if public_key.startswith("-----BEGIN PUBLIC KEY-----"):
            pem = public_key.encode("ascii")
        else:
            raw = base64.b64decode(public_key, validate=True)
            encoded = base64.b64encode(raw)
            lines = [encoded[i:i + 64] for i in range(0, len(encoded), 64)]
            pem = b"-----BEGIN PUBLIC KEY-----\n" + b"\n".join(lines) + b"\n-----END PUBLIC KEY-----\n"
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="jjz-public-key-") as directory:
            key_path = Path(directory) / "public.pem"
            key_path.write_bytes(pem)
            key_path.chmod(0o600)
            details = subprocess.run(
                [executable, "pkey", "-pubin", "-in", str(key_path), "-text", "-noout"],
                capture_output=True, timeout=10, check=False,
            )
            text = details.stdout.decode("ascii", errors="replace")
            match = re.search(r"Public-Key:\s*\((\d+) bit\)", text)
            if (details.returncode or match is None or "Modulus:" not in text or "Exponent:" not in text):
                raise ValueError
            bits = int(match.group(1))
            if not 1024 <= bits <= 8192:
                raise ValueError
            block_size = (bits + 7) // 8 - 11
            blocks, offset = [], 0
            while offset < len(payload):
                end = min(offset + block_size, len(payload))
                while end < len(payload) and payload[end] & 0xC0 == 0x80:
                    end -= 1
                operation = subprocess.run(
                    [executable, "pkeyutl", "-encrypt", "-pubin", "-inkey", str(key_path),
                     "-pkeyopt", "rsa_padding_mode:pkcs1"],
                    input=payload[offset:end], capture_output=True, timeout=10, check=False,
                )
                if operation.returncode or len(operation.stdout) != (bits + 7) // 8:
                    raise ValueError
                blocks.append(operation.stdout)
                offset = end
            encrypted = b"".join(blocks)
        return base64.b64encode(encrypted).decode("ascii")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, UnicodeError, OverflowError):
        raise AuthError("登录公钥或加密数据格式不符合预期。") from None


class LoginGateway:
    def __init__(self, timeout=20):
        if type(timeout) not in (int, float) or not 1 <= timeout <= 20:
            raise AuthError("登录超时设置必须在 1 至 20 秒之间。")
        self.timeout = float(timeout)
        self.cookies = CookieJar()
        self.opener = build_opener(_OfficialRedirect(), HTTPCookieProcessor(self.cookies))
        self._public_key = None
        self._entry_url = None
        self._ready = False
        self._certificate_failed = False

    def _raw(self, url: str, data: dict | None = None, *, allow_bad_request=False):
        official_url(url)
        headers = {"Accept": "application/json,text/html,image/*", "User-Agent": "jjz-renewer/0.1"}
        body = None
        if data is not None:
            # Mutation paths cannot be supplied by the caller or redirect to a new origin with a body.
            headers.update({"X-Requested-With": "XMLHttpRequest",
                            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                            "Origin": PORTAL_ORIGIN,
                            "Referer": self._entry_url or PORTAL_ORIGIN + "/m/index.html"})
            body = urlencode(data).encode("ascii")
        request = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                if error.code == 400 and allow_bad_request:
                    response = error
                else:
                    error.close()
                    raise AuthError("官方登录服务返回请求错误，请稍后重试。", category="network") from None
            with response:
                final_url = official_url(response.geturl())
                response_body = response.read(MAX_RESPONSE_BYTES + 1)
        except AuthError:
            raise
        except (URLError, OSError, HTTPException) as error:
            if _certificate_error(error):
                self._ready = False
                self._certificate_failed = True
                raise AuthError("官方登录或业务回调证书校验失败，已停止登录，请勿关闭证书校验。",
                                category="certificate") from None
            raise AuthError("官方登录服务连接失败或超时。", category="network") from None
        if len(response_body) > MAX_RESPONSE_BYTES:
            raise AuthError("官方登录响应超出大小限制。")
        return final_url, response_body

    def preflight(self) -> dict:
        """Establish the official session and verify callback TLS before any personal data."""
        self._ready = False
        self._public_key = None
        self._entry_url = None
        self._certificate_failed = False
        try:
            final_url, _ = self._raw(ENTRY_URL)
            parsed = urlsplit(final_url)
            if parsed.hostname != "portal.bjt.beijing.gov.cn" or parsed.path != "/m/index.html":
                raise AuthError("官方登录入口返回了未支持的页面。")
            # The official redirect leaves Base64 '+' unescaped. Unlike form
            # decoding, its H5 reader uses decodeURIComponent and preserves '+'.
            keys = [unquote(part.partition("=")[2]) for part in parsed.query.split("&")
                    if unquote(part.partition("=")[0]) == "pubKey"]
            if len(keys) != 1 or not keys[0] or len(keys[0]) > 16384:
                raise AuthError("官方登录页面未返回有效的会话公钥。")
            self._entry_url, self._public_key = final_url, keys[0]
            self._raw(CALLBACK_URL, allow_bad_request=True)
            self._raw(BUSINESS_ENTRY_URL)
        except AuthError:
            self._public_key = None
            raise
        self._ready = True
        return {"entry_reachable": True, "callback_reachable": True,
                "business_reachable": True, "certificate_valid": True}

    def _require_ready(self):
        if self._certificate_failed:
            raise AuthError("官方登录或业务回调证书校验失败，已停止登录，请勿关闭证书校验。",
                            category="certificate")
        if not self._ready or not self._public_key:
            raise AuthError("请先完成官方登录入口和业务回调的安全预检。")

    def captcha(self) -> bytes:
        self._require_ready()
        _, data = self._raw(API_ROOT + AUTH_CAPTCHA_PATH + "?" + str(time.time_ns() // 1_000_000))
        if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")
                or data.startswith((b"GIF87a", b"GIF89a"))):
            raise AuthError("官方服务未返回可识别的图形验证码。")
        return data

    def _post(self, path: str, fields: dict) -> dict:
        self._require_ready()
        _, response = self._raw(API_ROOT + path, fields, allow_bad_request=True)
        try:
            envelope = json.loads(response)
        except (ValueError, UnicodeError, RecursionError):
            raise AuthError("官方登录响应不是有效 JSON。") from None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("meta"), dict):
            raise AuthError("官方登录响应结构不符合预期。")
        code = envelope["meta"].get("code")
        if type(code) not in (int, str):
            raise AuthError("官方登录响应状态码不符合预期。")
        if code not in (0, "0"):
            if code in (5016, "5016", 5017, "5017", 5008, "5008"):
                raise AuthError("验证码错误或已过期，请重新获取并由本人输入。", category="verification")
            if code in (5020, "5020", 5022, "5022"):
                self._ready = False
                raise AuthError("官方登录会话已失效，请重新开始登录。", category="credentials")
            raise AuthError("官方平台拒绝登录请求，请在官方 App 核对账号状态。", category="credentials")
        return envelope

    def send_sms(self, phone: str, captcha: str) -> None:
        self._require_ready()
        self._post(AUTH_SEND_SMS_PATH, {
            "mobile": _phone(phone), "captcha": _challenge(captcha),
            "type": "login", "smsType": "login_type",
        })

    def _login_result(self, envelope: dict) -> dict:
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise AuthError("官方登录结果缺少认证信息。")
        cert_level, grant_level = data.get("certLevel"), data.get("grantLevel")
        known_levels = {"L1", "L2", "L3", "L31", "L32", "L33"}
        def numeric_level(value):
            if type(value) is int and 0 <= value <= 3:
                return value
            if isinstance(value, str) and re.fullmatch(r"[0-3]", value):
                return int(value)
            raise AuthError("官方登录结果的认证等级格式不符合预期。")
        if isinstance(cert_level, str) and cert_level in known_levels:
            if not isinstance(grant_level, str) or grant_level not in known_levels:
                raise AuthError("官方登录结果的认证等级格式不符合预期。")
            # The public official H5 compares these known L-level strings directly.
        else:
            cert_level, grant_level = numeric_level(cert_level), numeric_level(grant_level)
        if cert_level < grant_level:
            raise AuthError("当前账号实名认证等级不足，请由本人在京通 App 完成官方认证。",
                            category="identity")
        redirect_url = official_url(data.get("redirectUrl"))
        return {"redirect_url": redirect_url, "cert_level": cert_level}

    def login_password(self, phone: str, password: str, captcha: str) -> dict:
        self._require_ready()
        phone, captcha = _phone(phone), _challenge(captcha)
        if not isinstance(password, str) or not password or len(password) > 1024:
            raise AuthError("请在本机输入有效密码。", category="credentials")
        encrypted = _encrypt(self._public_key, {
            "userIdentity": phone,
            # This hash is mandated by the observed official wire protocol, not password storage.
            "encryptedPwd": hashlib.md5(password.encode("utf-8")).hexdigest(),
            "resetFlag": False,
        })
        return self._login_result(self._post(AUTH_PASSWORD_LOGIN_PATH, {
            "encryptData": encrypted, "captcha": captcha,
        }))

    def login_sms(self, phone: str, sms_code: str) -> dict:
        self._require_ready()
        encrypted = _encrypt(self._public_key, {"mobile": _phone(phone), "smsCode": _challenge(sms_code, sms=True)})
        return self._login_result(self._post(AUTH_SMS_LOGIN_PATH, {"encryptData": encrypted}))

    def take_callback(self, result: dict) -> tuple[str, bytes]:
        """Follow only the verified official redirect; the result is still NOT a permit AUTH."""
        self._require_ready()
        if not isinstance(result, dict):
            raise AuthError("缺少已验证的官方登录跳转结果。")
        url = official_url(result.get("redirect_url"))
        return self._raw(url, allow_bad_request=True)

    def follow_callback(self, result: dict) -> dict:
        final_url, _ = self.take_callback(result)
        return {"final_url": final_url}
