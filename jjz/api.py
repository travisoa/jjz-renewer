"""Small HTTP adapter for the reference protocol; never logs response bodies."""

from __future__ import annotations

import json
import socket
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlencode

from .config import Settings


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
# Authentication endpoints verified against the active official prod_new H5.
UNIFIED_AUTH_CALLBACK_URL = "https://ssp.jtgl.beijing.gov.cn/sso/callback/userauth"
UNIFIED_AUTH_ENTRY_URL = (
    "https://bjt.beijing.gov.cn/renzheng/open/m/login/goUserLogin?"
    "client_id=100100001423&redirect_uri="
    "https%3A%2F%2Fssp.jtgl.beijing.gov.cn%2Fsso%2Fcallback%2Fuserauth&"
    "response_type=code&scope=user_info&state=300001"
)
UNIFIED_AUTH_API_ROOT = "https://bjt.beijing.gov.cn/renzheng/"
UNIFIED_AUTH_PORTAL_ORIGIN = "https://portal.bjt.beijing.gov.cn"
AUTH_CAPTCHA_PATH = "common/generateCaptcha"
AUTH_SEND_SMS_PATH = "inner/m/sendSmsBeforeLogin"
AUTH_PASSWORD_LOGIN_PATH = "inner/m/login/doUserLoginByPwd"
AUTH_SMS_LOGIN_PATH = "inner/m/login/doUserLoginBySms"
BUSINESS_LOGIN_ORIGIN = "https://jjz.jtgl.beijing.gov.cn:2443"
BUSINESS_LOGIN_PAGE = BUSINESS_LOGIN_ORIGIN + "/login_h5_byUserCenter.html"
AUTH_ALLOWED_ORIGINS = frozenset({
    ("bjt.beijing.gov.cn", 443), ("portal.bjt.beijing.gov.cn", 443),
    ("ssp.jtgl.beijing.gov.cn", 443), ("jjz.jtgl.beijing.gov.cn", 2443),
})
BUSINESS_LOGIN_PATHS = {
    "/login_h5_byUserCenter.html": "/auth/userController/loginUserByUserCenter",
    "/login_h5.html": "/auth/userController/loginUser",
}
H5_SOURCE = "99c4g1a438jgf412sa3xvckd43256h7g"
LOGIN_SOURCES = frozenset({
    "cd11fd45727a4c28a6ff5e117f703922", "4cb05695749c44eaa625d01aaff55f3c",
    "6ff67657da8346ddab418205e0442a64", "18dc606fcbe34451a93d6501a2f90037",
    "d05bcc4b282a4f09a240936cb50aa5f6", "1d57be6de8c741638a3a6db72b42bd11",
    "7dc471ab17ba4320ae729dfa56491d8e", H5_SOURCE,
})
STATE_PATH = "/pro/applyRecordController/stateList"
SUBMIT_PATH = "/pro/applyRecordController/insertApplyRecord"
# Read-only checks the official H5 apply pages call before insertApplyRecord.
VEHICLE_CHECK_PATH = "/pro/applyRecordController/applyVehicleCheck"
DRIVER_PATH = "/pro/applyRecordController/getJsrxx"
SITE_CONFIG_PATH = "/pro/configRecordController/getConfigRecordInfo"
COUNT_CHECK_PATH = "/pro/applyRecordController/applyCheckNum"
DATES_PATH = "/pro/applyRecordController/checkHandle"
ROUTE_PATH = "/pro/applyRecordController/checkInputRoadInfo"
CHECK_PATHS = frozenset({VEHICLE_CHECK_PATH, DRIVER_PATH, SITE_CONFIG_PATH,
                         COUNT_CHECK_PATH, DATES_PATH, ROUTE_PATH})


class ApiError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False, auth_expired: bool = False):
        super().__init__(message)
        self.uncertain = uncertain
        self.auth_expired = auth_expired


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def authorization_value(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 16_384 or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise ApiError("登录结果未包含有效的业务凭证。")
    return value


class BusinessLoginClient:
    """Exchange only tickets from the two published official landing pages."""

    def __init__(self, timeout=20):
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    def exchange(self, landing_path, token, state):
        if landing_path not in BUSINESS_LOGIN_PATHS:
            raise ApiError("未支持的官方登录落地页。")
        authorization_value(token)
        if (not isinstance(state, str) or not state or len(state) > 128
                or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in state)):
            raise ApiError("官方登录渠道参数不符合预期。")
        url = BUSINESS_LOGIN_ORIGIN + BUSINESS_LOGIN_PATHS[landing_path] + "?" + urlencode({"state": state})
        request = Request(url, data=json.dumps({"token": token, "state": state}).encode("utf-8"),
                          headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "source": state if state in LOGIN_SOURCES else H5_SOURCE,
                                   "Origin": BUSINESS_LOGIN_ORIGIN,
                                   "Referer": BUSINESS_LOGIN_ORIGIN + landing_path}, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            auth_expired = error.code in (401, 403)
            error.close()
            raise ApiError("官方业务登录请求被拒绝，请重新登录。", auth_expired=auth_expired) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ApiError("官方业务登录连接失败或超时。") from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise ApiError("官方业务登录响应超出大小限制。")
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError("官方业务登录响应不是有效 JSON。") from None
        if not isinstance(result, dict) or type(result.get("code")) not in (int, str):
            raise ApiError("官方业务登录响应结构不符合预期。")
        if result["code"] not in (200, "200"):
            raise ApiError("官方业务登录未通过，请在官方 App 核对。",
                           auth_expired=result["code"] in (401, 403, "401", "403"))
        data = result.get("data")
        if not isinstance(data, dict):
            raise ApiError("官方业务登录结果缺少业务凭证。")
        return authorization_value(data.get("accessToken"))


class ApiClient:
    def __init__(self, settings: Settings, *, authorization=None):
        self.settings = settings
        self._authorization = authorization_value(authorization) if authorization is not None else None
        self.opener = build_opener(_NoRedirect())

    def state(self) -> dict:
        return self._request(STATE_PATH, {}, mutation=False)

    def check(self, path: str, payload: dict) -> dict:
        if path not in CHECK_PATHS:
            raise ApiError("未支持的校验接口。")
        return self._request(path, payload, mutation=False)

    def submit(self, payload: dict) -> dict:
        return self._request(SUBMIT_PATH, payload, mutation=True)

    def _request(self, path: str, payload: dict, *, mutation: bool) -> dict:
        if self._authorization is None:
            self.settings.validate_live()
        else:
            self.settings.validate_service()
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ApiError("请求内容无法编码为 JSON。") from None
        request = Request(
            self.settings.base_url + path,
            data=body,
            headers={"Authorization": self._authorization if self._authorization is not None
                     else self.settings.read_authorization(),
                     "Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.settings.timeout_seconds) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            code = error.code
            error.close()
            if code in {401, 403}:
                raise ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True) from None
            # Redirects are never followed with an Authorization header.
            uncertain = mutation and (300 <= code < 400 or code >= 500)
            raise ApiError("接口 HTTP 请求失败。", uncertain=uncertain) from None
        except (URLError, TimeoutError, socket.timeout, OSError, HTTPException):
            raise ApiError("接口网络请求失败或超时。", uncertain=mutation) from None
        if len(body) > MAX_RESPONSE_BYTES:
            raise ApiError("接口响应超出大小限制。", uncertain=mutation)
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError("接口返回无效 JSON。", uncertain=mutation) from None
        if not isinstance(result, dict) or "code" not in result:
            raise ApiError("接口响应结构不符合预期。", uncertain=mutation)
        code = result["code"]
        if type(code) not in (int, str):
            raise ApiError("接口响应状态码不符合预期。", uncertain=mutation)
        if code in (401, 403, "401", "403"):
            raise ApiError("接口拒绝凭证，请更新本地 Authorization。", auth_expired=True)
        if code not in (200, "200"):
            raise ApiError("接口拒绝业务请求，请在官方 App 中核实。")
        return result
