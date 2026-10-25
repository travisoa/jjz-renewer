"""Local configuration and credential handling. No remote state is stored here."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import stat
import tomllib
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A configuration problem whose message is safe to display."""


# Fields the official H5 "进京信息" page adds to insertApplyRecord. Vehicle,
# driver, permit type and date come from the server at submission time.
REQUIRED_FIELDS = ("jjmd", "jjmdmc", "area", "jjdq", "xxdz", "jjdzgdjd", "jjdzgdwd",
                   "sfzj", "sqdzgdjd", "sqdzgdwd")
IN_BEIJING_FIELDS = ("zjxxdz", "zjxxdzgdjd", "zjxxdzgdwd")
ROAD_FIELDS = ("jjlk", "jjlkmc", "jjlkgdjd", "jjlkgdwd")
APPLICATION_FIELDS = frozenset(REQUIRED_FIELDS + IN_BEIJING_FIELDS + ROAD_FIELDS + ("txrxx",))
LONGITUDES = frozenset({"jjdzgdjd", "sqdzgdjd", "zjxxdzgdjd", "jjlkgdjd"})


def _origin(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() or c == "\\" for c in value)
        )
        # Accessing port validates its format and range.
        _ = parsed.port
        parsed.netloc.encode("ascii")
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ConfigError("api.base_url 必须是 HTTPS 服务源地址，不含账号、路径或查询参数。")
    return value.rstrip("/")


@dataclass
class Settings:
    config_path: Path
    base_url: str
    auth_file: Path
    plate: str
    default_permit: str = "outside"
    earliest_time: str = "08:00"
    outside_lead_days: int = 0
    submission_enabled: bool = False
    timeout_seconds: float = 20
    data_dir: Path = Path("data")
    application: dict = field(default_factory=dict)
    inside_application: dict = field(default_factory=dict)
    login_file: Path | None = None
    auto_relogin: bool = False

    def read_authorization(self) -> str:
        """Read a single local header value without ever including it in errors."""
        try:
            info = self.auth_file.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ConfigError("凭证文件必须是普通文件，且仅当前用户可读写（chmod 600）。")
            if info.st_size > 16_384:
                raise ConfigError("凭证文件过大。")
            value = self.auth_file.read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise ConfigError("无法读取本地凭证文件。") from None
        if not value or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ConfigError("凭证必须是非空单行 Authorization 值。")
        try:
            value.encode("ascii")
        except UnicodeError:
            raise ConfigError("凭证必须是 ASCII Authorization 值。") from None
        return value

    def read_login(self) -> tuple[str, str]:
        """Read the private phone/password pair; only login commands and auto_relogin call this."""
        if self.login_file is None:
            return "", ""
        try:
            info = self.login_file.stat()
        except FileNotFoundError:
            return "", ""
        except OSError:
            raise ConfigError("无法读取本地登录配置文件。") from None
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ConfigError("登录配置文件必须是普通文件，且仅当前用户可读写（chmod 600）。")
        if info.st_size > 16_384:
            raise ConfigError("登录配置文件过大。")
        try:
            with self.login_file.open("rb") as stream:
                data = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            raise ConfigError("无法读取登录配置文件，请检查 TOML 格式。") from None
        login = data.get("login", {})
        if data.keys() - {"login"} or not isinstance(login, dict) or login.keys() - {"phone", "password"}:
            raise ConfigError("登录配置文件只能包含 [login] 表的 phone 和 password。")
        phone, password = login.get("phone", ""), login.get("password", "")
        if not isinstance(phone, str) or not isinstance(password, str):
            raise ConfigError("login.phone 和 login.password 必须是字符串。")
        phone = phone.strip()
        if any(ord(c) < 32 or ord(c) == 127 for c in phone + password):
            raise ConfigError("login.phone 和 login.password 必须是单行文本。")
        return phone, password

    def validate_service(self) -> None:
        if not self.base_url:
            raise ConfigError("尚未配置 api.base_url。")
        _origin(self.base_url)
        if not self.plate:
            raise ConfigError("尚未配置 vehicle.plate。")

    def validate_live(self) -> None:
        self.validate_service()
        self.read_authorization()

    def application_for(self, permit: str = "outside") -> dict:
        if permit == "outside":
            return self.application
        if permit == "inside":
            return self.inside_application
        raise ConfigError("证件类型仅支持 outside 或 inside。")

    def validate_application(self, permit: str = "outside") -> None:
        application = self.application_for(permit)
        label = "inside_application" if permit == "inside" else "application"
        missing = [key for key in REQUIRED_FIELDS + ("txrxx",) if key not in application]
        unknown = application.keys() - APPLICATION_FIELDS
        if missing:
            raise ConfigError(label + " 缺少字段：" + ", ".join(missing))
        if unknown:
            # Never interpolate untrusted user keys or values into log output.
            raise ConfigError(label + " 含未支持字段；车辆、驾驶人、证件类型、申请日期由程序生成。")
        if application["sfzj"] not in {"0", "1"}:
            raise ConfigError(label + ".sfzj 必须为字符串 0 或 1。")
        # The official page sends either the in-Beijing address or the entry road.
        used, unused = ((IN_BEIJING_FIELDS, ROAD_FIELDS) if application["sfzj"] == "1"
                        else (ROAD_FIELDS, IN_BEIJING_FIELDS))
        for key in REQUIRED_FIELDS + used:
            value = application.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(label + "." + key + " 必须填写非空字符串。")
        for key in unused:
            if application.get(key, "") != "":
                raise ConfigError(label + "." + key + " 与 sfzj 的选择不符，应留空。")
        if not isinstance(application["txrxx"], list):
            raise ConfigError(label + ".txrxx 必须是数组。")
        codes, names = application["jjmd"].split(","), application["jjmdmc"].split(",")
        if not all(re.fullmatch(r"\d{2}", code) for code in codes) or len(codes) != len(names):
            raise ConfigError(label + ".jjmd 须为两位代码（多选用英文逗号分隔），并与 jjmdmc 一一对应。")
        if not re.fullmatch(r"\d{3}", application["jjdq"]):
            raise ConfigError(label + ".jjdq 必须是三位区县代码。")
        for key in (k for k in REQUIRED_FIELDS + used if k.endswith(("gdjd", "gdwd"))):
            limit = 180 if key in LONGITUDES else 90
            try:
                coordinate = Decimal(application[key])
                valid = coordinate.is_finite() and -limit <= coordinate <= limit
            except InvalidOperation:
                valid = False
            if not valid:
                raise ConfigError(label + "." + key + " 必须是有效经纬度。")


def load_config(path: Path | str) -> Settings:
    config_path = Path(path).expanduser().resolve()
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        raise ConfigError("无法读取 TOML 配置文件。") from None

    def section(name: str) -> dict:
        value = config.get(name, {})
        if not isinstance(value, dict):
            raise ConfigError(name + " 必须是 TOML 配置表。")
        return value

    def string(table: dict, key: str, default: str, label: str) -> str:
        value = table.get(key, default)
        if not isinstance(value, str):
            raise ConfigError(label + " 必须是字符串。")
        return value

    api = section("api")
    vehicle = section("vehicle")
    renewal = section("renewal")
    storage = section("storage")
    application = section("application")
    inside_application = section("inside_application")
    if "login" in config:
        # Keep the account password out of the file that every command loads.
        raise ConfigError("登录手机号和密码只能放在 secrets/login.toml，请把 config.toml 中的 [login] 整节移过去。")
    base_url = _origin(string(api, "base_url", "", "api.base_url"))
    auth_file = string(api, "auth_file", "secrets/authorization.txt", "api.auth_file")
    login_file = string(api, "login_file", "secrets/login.toml", "api.login_file")
    data_dir = string(storage, "data_dir", "data", "storage.data_dir")
    if not auth_file.strip() or not login_file.strip() or not data_dir.strip():
        raise ConfigError("凭证与数据路径不能为空。")
    plate = string(vehicle, "plate", "", "vehicle.plate").strip().upper()
    if any(ord(c) < 32 for c in plate):
        raise ConfigError("vehicle.plate 含无效字符。")
    permit = string(vehicle, "default_permit", "outside", "vehicle.default_permit")
    if permit not in {"outside", "inside"}:
        raise ConfigError("vehicle.default_permit 仅支持 outside 或 inside。")
    earliest = string(renewal, "earliest_time", "08:00", "renewal.earliest_time")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", earliest):
        raise ConfigError("renewal.earliest_time 必须是 HH:MM 时间。")
    lead = renewal.get("outside_lead_days", 0)
    if type(lead) is not int or lead not in (0, 1):
        raise ConfigError("renewal.outside_lead_days 仅支持整数 0 或 1。")
    enabled = renewal.get("submission_enabled", False)
    if type(enabled) is not bool:
        raise ConfigError("renewal.submission_enabled 必须是布尔值。")
    relogin = api.get("auto_relogin", False)
    if type(relogin) is not bool:
        raise ConfigError("api.auto_relogin 必须是布尔值。")
    timeout = api.get("timeout_seconds", 20)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
        raise ConfigError("api.timeout_seconds 必须在 1 至 120 秒之间。")

    def local_path(value: str) -> Path:
        expanded = Path(value).expanduser()
        return (config_path.parent / expanded).resolve()

    return Settings(
        config_path=config_path, base_url=base_url, auth_file=local_path(auth_file),
        plate=plate, default_permit=permit, earliest_time=earliest,
        outside_lead_days=lead, submission_enabled=enabled,
        timeout_seconds=float(timeout), data_dir=local_path(data_dir),
        application=dict(application), inside_application=dict(inside_application),
        login_file=local_path(login_file), auto_relogin=relogin,
    )
