"""Verify a phone-login business credential and save it atomically.

The new credential is checked against the configured vehicle while it is
still in memory. The existing authorization file is never read or copied.
"""

import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote, urlsplit

from .api import ApiClient, ApiError, BUSINESS_LOGIN_PATHS, SERVICE_ORIGINS, authorization_value
from .auth import AuthError
from .config import ConfigError
from .domain import normalize


def validate_phone_login_settings(settings):
    settings.validate_service()
    if settings.base_url not in SERVICE_ORIGINS:
        raise ConfigError("手机号登录只支持已核对的官方进京证服务源。")
    if settings.plate == "冀A00000":
        raise ConfigError("请先在配置中填写本人的真实车牌。")


def callback_ticket(url):
    """Select a protocol only from the official, actual callback landing URL."""
    try:
        if not isinstance(url, str) or any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == "jjz.jtgl.beijing.gov.cn"
                 and parsed.port == 2443 and parsed.username is None and parsed.password is None
                 and parsed.path in BUSINESS_LOGIN_PATHS and not parsed.fragment)
        if not valid:
            raise ValueError
        values = {}
        for part in parsed.query.split("&"):
            key, _, value = part.partition("=")
            if re.search(r"%(?![0-9a-fA-F]{2})", key + value):
                raise ValueError
            key, value = unquote(key, errors="strict"), unquote(value, errors="strict")
            values.setdefault(key, []).append(value)
        tokens, states = values.get("token", []), values.get("state", ["101000004071"])
        if len(tokens) != 1 or len(states) != 1 or not tokens[0] or not states[0]:
            raise ValueError
        # Preserve Base64 '+' exactly, matching decodeURIComponent in the H5.
        authorization_value(tokens[0])
    except (TypeError, ValueError, UnicodeError, ApiError):
        raise AuthError("官方回调未返回已知业务登录页和唯一票据，已停止，请核对实际登录流程。") from None
    return parsed.path, tokens[0], states[0]


def verify_phone_authorization(settings, authorization, client_factory=None):
    """Return the state response once it shows the configured vehicle."""
    validate_phone_login_settings(settings)
    # The new credential stays in memory. Do not inspect or temporarily copy
    # the existing authorization file merely to test a new login.
    client = (client_factory or ApiClient)(settings, authorization=authorization)
    response = client.state()
    normalize(response, settings.plate)
    return response


def save_authorization(settings, authorization):
    """Atomically replace the credential with a 0600 file; called only by auth-set."""
    authorization_value(authorization)
    settings.auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".authorization-", dir=settings.auth_file.parent)
    candidate = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            os.fchmod(target.fileno(), 0o600)
            target.write(authorization)
            target.flush()
            os.fsync(target.fileno())
        os.replace(candidate, settings.auth_file)
    finally:
        candidate.unlink(missing_ok=True)
