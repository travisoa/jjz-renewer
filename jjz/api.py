"""Small HTTP adapter for the reference protocol; never logs response bodies."""

from __future__ import annotations

import json
import socket
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Settings


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
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


class ApiClient:
    def __init__(self, settings: Settings):
        self.settings = settings
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
        self.settings.validate_live()
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ApiError("请求内容无法编码为 JSON。") from None
        request = Request(
            self.settings.base_url + path,
            data=body,
            headers={"Authorization": self.settings.read_authorization(),
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
