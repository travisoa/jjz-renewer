"""Import a business credential from the user's local capture, then verify it.

Only the explicitly selected stateList request is read. General login tokens,
cookies and identity-bearing response bodies are not credential candidates.
"""

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .api import ApiClient, STATE_PATH
from .config import ConfigError, _origin
from .domain import normalize


def extract_authorization(har_path, service_origin):
    origin = _origin(service_origin)
    if not origin:
        raise ConfigError("必须明确指定本人办理服务的 HTTPS 源地址")
    path = Path(har_path)
    try:
        if path.stat().st_size > 50 * 1024 * 1024:
            raise ConfigError("HAR 文件超过 50 MiB，请仅导出所需办理请求")
        contents = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ConfigError("无法读取 HAR 文件，请检查本地路径和文件格式") from None
    log = contents.get("log") if isinstance(contents, dict) else None
    entries = log.get("entries") if isinstance(log, dict) else None
    if not isinstance(entries, list):
        raise ConfigError("HAR 缺少 log.entries 请求列表")
    candidates = set()
    for entry in entries:
        request = entry.get("request") if isinstance(entry, dict) else None
        if not isinstance(request, dict) or request.get("method") != "POST":
            continue
        url = request.get("url")
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        # The official H5 joins "/pro/" and "/applyRecordController/..." into "//".
        if re.sub(r"/{2,}", "/", parsed.path) != STATE_PATH:
            continue
        try:
            candidate_origin = _origin(f"{parsed.scheme}://{parsed.netloc}")
        except ConfigError:
            continue
        if candidate_origin != origin:
            continue
        headers = request.get("headers", [])
        if not isinstance(headers, list):
            raise ConfigError("目标请求的 headers 格式错误")
        for header in headers:
            if not isinstance(header, dict):
                continue
            name, value = header.get("name"), header.get("value")
            if isinstance(name, str) and name.lower() == "authorization":
                if not isinstance(value, str) or not value or value != value.strip():
                    raise ConfigError("目标请求 Authorization 格式错误")
                if any(ord(c) < 32 or ord(c) > 126 for c in value) or len(value) > 16_384:
                    raise ConfigError("目标请求 Authorization 格式错误")
                candidates.add(value)
    if len(candidates) != 1:
        raise ConfigError("未找到唯一的目标 stateList 凭证，请仅导出一次本人有效查询")
    return candidates.pop()


def verify_and_save(settings, service_origin, authorization, client_factory=ApiClient):
    """Keep the old credential intact until exact-vehicle verification succeeds."""
    origin = _origin(service_origin)
    if not origin or (settings.base_url and settings.base_url != origin):
        raise ConfigError("指定服务源与已有 api.base_url 不一致，请先核对本人配置")
    if not settings.plate or settings.plate == "冀A00000":
        raise ConfigError("请先在配置中填写本人的真实车牌")
    # Make the service destination an explicit config choice rather than silently
    # deriving it from a potentially stale/mixed capture.
    if not settings.base_url:
        raise ConfigError("请先将已核对的服务源写入 api.base_url")
    settings.auth_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".authorization-", dir=settings.auth_file.parent)
    candidate_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(authorization)
            stream.flush()
            os.fsync(stream.fileno())
        candidate = replace(settings, base_url=origin, auth_file=candidate_path)
        candidate.validate_live()
        records = normalize(client_factory(candidate).state(), settings.plate)
        os.replace(candidate_path, settings.auth_file)
        return {"action": "business_authorization_verified", "records": len(records),
                "submission_enabled": settings.submission_enabled, "mode": "0600"}
    finally:
        candidate_path.unlink(missing_ok=True)
