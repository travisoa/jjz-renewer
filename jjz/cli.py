"""Small local CLI; every output is a summary, never a raw server response."""

import argparse
from datetime import datetime
from getpass import getpass
from importlib.resources import files
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import sys
import tempfile
import webbrowser
from zoneinfo import ZoneInfo

from .api import ApiClient, ApiError, BusinessLoginClient
from .auth import AuthError, LoginGateway
from .captcha_ocr import recognize_captcha
from .config import ConfigError, config_for_vehicle, load_config, rewrite_config
from .credentials import (callback_ticket, save_authorization, validate_phone_login_settings,
                          verify_phone_authorization)
from .domain import DomainError, account_vehicles, normalize, plan, record_type, select_vehicle
from .runtime import AVAILABLE_FLAG, describe, execute, mask_plate
from .storage import BusyError, Journal, credential_lock, exclusive


BEIJING = ZoneInfo("Asia/Shanghai")
# Names .gitignore protects and launchd accepts in a job label.
CONFIG_NAME = re.compile(r"config(?:-[A-Za-z0-9_-]+)?\.toml")
CONFIG_NAME_RULE = "配置文件名须为 config.toml 或 config-<名称>.toml，名称只用英文字母、数字、- 和 _"


def emit(data):
    print(json.dumps(data, ensure_ascii=False, sort_keys=True))


def notice(message):
    """Human-readable progress; stdout carries only the final JSON line."""
    print(message, file=sys.stderr)


def _remaining_count(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)


def parser():
    root = argparse.ArgumentParser(description="进京证本地续办工具，默认只预览")
    root.add_argument("--config", default="config.toml", help="配置路径，默认 config.toml")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="按示例补建 config.toml 和 secrets/login.toml，不覆盖已有文件")
    sub.add_parser("doctor", help="本地检查，不发起网络请求")
    auth_set = sub.add_parser("auth-set", help="本人在本机交互输入或手机号登录并保存业务凭证")
    auth_set.add_argument("--login", action="store_true", help="通过本人手机号登录并验证业务凭证")
    auth_set.add_argument("--method", choices=["password", "sms"], default="password")
    sub.add_parser("login-check", help="只读检查官方手机号登录入口及回调证书，不输入账号")
    login = sub.add_parser("login", help="交互式手机号登录并查询目标车辆验证，不保存凭证")
    login.add_argument("--method", choices=["password", "sms"], default="password")
    sub.add_parser("status", help="只读查询当前证件状态（输出已脱敏）")
    vehicles = sub.add_parser("vehicles", help="列出账号下绑定的车辆及对应配置（输出已脱敏）")
    vehicles.add_argument("--create", action="store_true", help="为还没有配置的车辆生成配置，不改动已有文件")
    preview = sub.add_parser("plan", help="只读查询并给出续办计划")
    preview.add_argument("--permit", choices=["outside", "inside"])
    preview.add_argument("--fixture", type=Path, help="使用本地响应样本，不联网")
    preview.add_argument("--at", help="仅离线样本可指定模拟时间（ISO 8601）")
    run = sub.add_parser("run", help="查询并执行一次；默认仅预览")
    run.add_argument("--permit", choices=["outside", "inside"])
    run.add_argument("--submit", action="store_true", help="按配置实际提交符合条件的续办")
    run.add_argument("--scheduled", action="store_true", help="定时任务使用：按需办理（auto_renew = false）的车直接跳过")
    sub.add_parser("history", help="查看本地脱敏提交记录")
    resolve = sub.add_parser("resolve", help="在 App 确认未提交后，解除一条本地提交保护")
    resolve.add_argument("--attempt", required=True)
    resolve.add_argument("--confirmed-not-submitted", action="store_true", required=True)
    service = sub.add_parser("schedule", help="生成 macOS 定时任务 plist（不自动加载）")
    service.add_argument("--output", type=Path, help="默认写到配置的数据目录，文件名为任务名")
    service.add_argument("--interval", type=int, default=1800, help="检查间隔秒，最小 900")
    service.add_argument("--submit", action="store_true", help="生成实际提交任务；仍受配置开关约束")
    return root


def render_schedule(settings, args):
    if not 900 <= args.interval <= 86400:
        raise ConfigError("调度间隔须在 900 到 86400 秒之间")
    if not settings.auto_renew:
        raise ConfigError("这辆车设为按需办理（renewal.auto_renew = false），不生成定时任务")
    # One config per vehicle: the config file name keeps each vehicle's job distinct.
    if not CONFIG_NAME.fullmatch(settings.config_path.name):
        raise ConfigError(CONFIG_NAME_RULE)
    label = "local.jjz.renew." + settings.config_path.stem
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A pinned outside target keeps manual inside requests out of the background job.
    # --scheduled lets a later switch to on-demand stop the job without reloading it.
    command = [sys.executable, "-m", "jjz", "--config", str(settings.config_path),
               "run", "--scheduled", "--permit", "outside"]
    if args.submit:
        command.append("--submit")
    project = Path(__file__).resolve().parent.parent
    payload = {
        "Label": label,
        "ProgramArguments": command,
        "WorkingDirectory": str(project),
        "StartInterval": args.interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "Umask": 0o077,
        "StandardOutPath": str(settings.data_dir / "launchd.log"),
        "StandardErrorPath": str(settings.data_dir / "launchd.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
    }
    path = (args.output or settings.data_dir / (label + ".plist")).expanduser().resolve()
    private_write(path, plistlib.dumps(payload))
    return {"action": "schedule_created", "label": label, "path": str(path), "interval_seconds": args.interval,
            "submission": args.submit, "permit": "outside", "loaded": False}


def _ask_captcha(settings, image):
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="login-", dir=settings.data_dir) as folder:
        suffix = ".png" if image.startswith(b"\x89PNG") else ".jpg" if image.startswith(b"\xff\xd8") else ".gif"
        captcha_path = Path(folder) / ("captcha" + suffix)
        private_write(captcha_path, image)
        opened = webbrowser.open(captcha_path.as_uri())
        if not opened:
            notice("请打开验证码图片：" + str(captcha_path))
        return getpass("请本人识别图形验证码并输入（隐藏输入）: ").strip()


def _with_graphic_captcha(gateway, settings, submit, *, unattended=False):
    """Try a local guess up to three times, then ask the user once; unattended runs never ask."""
    for _ in range(3):
        image = gateway.captcha()
        captcha = recognize_captcha(image)
        if not captcha:
            if unattended:
                continue
            return submit(_ask_captcha(settings, image))
        notice("图形验证码已由本地识别填写。")
        try:
            return submit(captcha)
        except AuthError as error:
            if error.category != "verification":
                raise
            notice("图形验证码未通过，正在重新识别。")
    if unattended:
        raise AuthError("本机未能识别图形验证码，自动重新登录已停止。", category="verification")
    return submit(_ask_captcha(settings, gateway.captcha()))


def _open_login(settings, gateway):
    gateway = gateway or LoginGateway(timeout=min(settings.timeout_seconds, 20))
    gateway.preflight() # No personal data or SMS before all HTTPS checks pass.
    # Using this project means the user accepts the official login agreement.
    notice("登录协议请在官方页面阅读：https://portal.bjt.beijing.gov.cn/m/index.html")
    notice("使用本项目即视为已阅读并同意官方登录协议，继续登录。")
    return gateway


def _can_apply(vehicle):
    # The official permit-type page enables a type only when its flag is true.
    return {permit: vehicle.get(flag) is True for permit, flag in AVAILABLE_FLAG.items()}


def _vehicle_configs(settings, *, skip_unreadable=False):
    """Load the given config and every config.toml or config-*.toml beside it."""
    folder = settings.config_path.parent
    configs = {settings.config_path: settings}
    for path in sorted([*folder.glob("config.toml"), *folder.glob("config-*.toml")]):
        path = path.resolve()
        if path in configs:
            continue
        try:
            configs[path] = load_config(path)
        except ConfigError as error:
            if skip_unreadable:
                continue  # a config that cannot load cannot submit either
            # An unreadable config may already cover a vehicle; never guess.
            raise ConfigError(path.name + "：" + str(error)) from None
    return configs


def _same_vehicle_configs(settings):
    """Names of the other configs beside this one for the same plate."""
    if not settings.plate:
        return []
    return [path.name for path, config in _vehicle_configs(settings, skip_unreadable=True).items()
            if path != settings.config_path and config.plate == settings.plate]


def sync_vehicles(settings, response, *, create=False):
    """Match the account's vehicles to config files; with create, add one for each vehicle without.

    A new config copies the given one with both switches off and its own data
    directory. Existing files are never changed.
    """
    account = account_vehicles(response)
    configs = _vehicle_configs(settings)
    covering = {}
    for path, config in configs.items():
        covering.setdefault(config.plate, []).append(path)
    folder = settings.config_path.parent
    taken = {config.data_dir for config in configs.values()}
    base_text, number, created, vehicles = None, 2, 0, []
    for plate, vehicle in account:
        entries = [{"file": path.name, "auto_renew": configs[path].auto_renew,
                    "submission_enabled": configs[path].submission_enabled}
                   for path in covering.get(plate, [])]
        if create and not entries:
            if base_text is None:
                base_text = settings.config_path.read_text(encoding="utf-8")
            while True:
                path = folder / f"config-car{number}.toml"
                data_dir = (folder / "data" / f"car{number}").resolve()
                if not (os.path.lexists(path) or data_dir.exists() or data_dir in taken):
                    break
                number += 1
            private_write(path, config_for_vehicle(base_text, plate, f"data/car{number}").encode("utf-8"))
            taken.add(data_dir)
            created += 1
            entries = [{"file": path.name, "auto_renew": False, "submission_enabled": False, "created": True}]
        vehicles.append({"vehicle": mask_plate(plate), "can_apply": _can_apply(vehicle), "configs": entries})
    plates = {plate for plate, _ in account}
    # Configs of other accounts use other credential files and are not expected here.
    unmatched = [{"file": path.name, "vehicle": mask_plate(config.plate)} for path, config in configs.items()
                 if config.auth_file == settings.auth_file and config.plate not in plates]
    # Counted over every config, bound or not, as run refuses each of them alike.
    duplicates = [[path.name for path in paths] for plate, paths in covering.items() if plate and len(paths) > 1]
    missing = sum(1 for item in vehicles if not item["configs"])
    if created:
        reason = f"已为 {created} 辆车生成配置，提交开关和自动续办都已关闭，核对申请模板后按需开启"
    elif missing:
        reason = f"{missing} 辆车还没有配置，运行 vehicles --create 生成"
    else:
        reason = "账号下的车辆都已有配置"
    if duplicates:
        reason += "；同一辆车有多份配置，提交记录互不相通，请只保留一份"
    if unmatched:
        reason += "；有配置的车辆不在当前账号中，请到 App 核对"
    return {"vehicles": vehicles, "unmatched_configs": unmatched, "duplicates": duplicates, "reason": reason}


def _create_configs(settings, response):
    """After an interactive login, add configs for vehicles without one; the credential stays saved."""
    try:
        result = sync_vehicles(settings, response, create=True)
    except (ConfigError, DomainError, OSError) as error:
        # Arbitrary OS errors may include private paths.
        return {"configs_error": "本地文件处理失败" if isinstance(error, OSError) else str(error)}
    return {"configs_created": [entry["file"] for item in result["vehicles"]
                                for entry in item["configs"] if entry.get("created")]}


def _finish_login(settings, gateway, result, *, save, create_configs=False):
    final_url, _ = gateway.take_callback(result)
    landing_path, ticket, state = callback_ticket(final_url)
    authorization = BusinessLoginClient(timeout=min(settings.timeout_seconds, 20)).exchange(
        landing_path, ticket, state)
    response = verify_phone_authorization(settings, authorization)
    summary = {"action": "business_login_verified", "business_auth_verified": True,
               "credential_saved": False, "vehicle": mask_plate(settings.plate),
               "records": len(normalize(response, settings.plate))}
    if save:
        # Saved only after the in-memory credential has queried the target vehicle.
        with exclusive(settings.data_dir):
            save_authorization(settings, authorization)
        summary["credential_saved"] = True
        if create_configs:
            summary.update(_create_configs(settings, response))
    return summary


def _relogin_account(settings):
    phone, password = settings.read_login()
    if not phone or not password:
        raise ConfigError("自动重新登录需要在 secrets/login.toml 填写 login.phone 和 login.password。")
    return phone, password


def unattended_login(settings, gateway=None):
    """Password login from login_file with local captcha recognition only; never prompts."""
    validate_phone_login_settings(settings)
    phone, password = _relogin_account(settings)
    gateway = _open_login(settings, gateway)
    result = _with_graphic_captcha(
        gateway, settings, lambda captcha: gateway.login_password(phone, password, captcha), unattended=True)
    return _finish_login(settings, gateway, result, save=True)


class ReloginFailed(ApiError):
    """The credential stays rejected because the unattended login did not finish."""


def _credential_version(settings):
    """Identify the saved credential without reading it; every save replaces the file."""
    try:
        info = settings.auth_file.stat()
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


def with_relogin(settings, action, expired=lambda result: False):
    """Call action; after a rejected credential, log in once without prompts and call it again.

    Configs for other vehicles may share the credential file. Their logins wait
    for each other, and a credential saved by another config meanwhile is used
    without logging in again. Returns the result and the relogin outcome or None.
    """
    version = _credential_version(settings)
    try:
        result = action()
        if not (settings.auto_relogin and expired(result)):
            return result, None
    except ApiError as error:
        if not (settings.auto_relogin and error.auth_expired):
            raise
    try:
        with credential_lock(settings.auth_file,
                             on_wait=lambda: notice("使用同一凭证的其他配置正在重新登录，等待其完成。")):
            if _credential_version(settings) != version:
                notice("业务凭证已由使用同一凭证的其他配置更新，直接重新执行。")
                outcome = "shared"
            else:
                notice("业务凭证已失效，正在用登录配置自动重新登录一次。")
                unattended_login(settings)
                outcome = "succeeded"
    except (ConfigError, AuthError, ApiError, BusyError, DomainError, OSError) as error:
        # DomainError: the new login cannot see the vehicle, e.g. a login file of another account.
        # Arbitrary OS errors may include private paths.
        reason = "本地文件处理失败" if isinstance(error, OSError) else str(error)
        raise ReloginFailed("业务凭证已失效，自动重新登录未成功：" + reason, auth_expired=True) from None
    return action(), outcome


def interactive_login(settings, method, gateway=None, *, save=False, create_configs=False):
    if not sys.stdin.isatty():
        raise ConfigError("请在本机交互式终端运行 login，账号、密码和验证码不通过聊天或参数传入")
    validate_phone_login_settings(settings)
    phone, password = settings.read_login()
    password = password if method == "password" else None
    if phone and method == "password" and not password:
        raise ConfigError("已配置 login.phone 时，密码登录还需要在 secrets/login.toml 填写 login.password。")
    gateway = _open_login(settings, gateway)
    if not phone:
        phone = getpass("本人手机号（隐藏输入）: ").strip()
    if method == "password" and not password:
        password = getpass("登录密码（隐藏输入，不保存）: ")
    if method == "sms":
        # A wrong graphic captcha is retried locally. A sent SMS is never requested again.
        _with_graphic_captcha(gateway, settings, lambda captcha: gateway.send_sms(phone, captcha))
        sms_code = getpass("收到的短信验证码（隐藏输入，不保存）: ").strip()
        result = gateway.login_sms(phone, sms_code)
    else:
        result = _with_graphic_captcha(
            gateway, settings, lambda captcha: gateway.login_password(phone, password, captcha))
    return _finish_login(settings, gateway, result, save=save, create_configs=create_configs)


def main(argv=None):
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        config_path = Path(args.config).expanduser().resolve()
        if args.command == "init":
            if not CONFIG_NAME.fullmatch(config_path.name):
                raise ConfigError(CONFIG_NAME_RULE)
            # Create each missing private file from its packaged example; never overwrite.
            # An existing config decides where its login file lives.
            login_path = (load_config(config_path).login_file if config_path.exists()
                          else config_path.parent / "secrets/login.toml")
            templates = {config_path: "config.example.toml", login_path: "login.example.toml"}
            created = [path for path in templates if not path.exists()]
            for path in created:
                data = files("jjz").joinpath(templates[path]).read_bytes()
                if path == config_path and path.name != "config.toml":
                    # Like vehicles --create: another vehicle starts on demand and keeps its
                    # journal, lock and job log in its own directory.
                    changes = {("storage", "data_dir"): "data/" + path.stem.removeprefix("config-"),
                               ("renewal", "auto_renew"): False}
                    data = rewrite_config(data.decode("utf-8"), changes).encode("utf-8")
                private_write(path, data)
            if not created:
                emit({"action": "config_exists", "created": [], "reason": "配置文件和登录文件都已存在，未做修改"})
                return 0
            emit({"action": "config_created", "created": [str(path) for path in created], "submission": False})
            return 0
        if args.command == "login-check":
            emit({"action": "login_preflight", **LoginGateway().preflight(),
                  "personal_data_sent": False, "business_auth_verified": False})
            return 0
        settings = load_config(config_path)
        if args.command == "login":
            result = interactive_login(settings, args.method)
            emit(result)
            return 0
        if args.command == "doctor":
            checks = {}
            items = [("live_query", settings.validate_live),
                     ("outside_application", settings.validate_application),
                     ("inside_application", lambda: settings.validate_application("inside"))]
            if settings.auto_relogin:
                items.append(("auto_relogin", lambda: _relogin_account(settings)))
            for name, check in items:
                try:
                    check()
                    checks[name] = "ready"
                except ConfigError as error:
                    checks[name] = str(error)
            emit({"action": "doctor", "checks": checks,
                  "submission_enabled": settings.submission_enabled, "auto_renew": settings.auto_renew,
                  "vehicle": mask_plate(settings.plate), "timezone": "Asia/Shanghai",
                  "network_checked": False})
            return 0 if all(value == "ready" for value in checks.values()) else 2
        if args.command == "auth-set":
            if not sys.stdin.isatty():
                raise ConfigError("请在本机交互式终端运行 auth-set，避免明文进入命令和日志")
            if args.login:
                # Only this interactive login adds configs; the unattended one never writes them.
                emit(interactive_login(settings, args.method, save=True, create_configs=True))
                return 0
            token = getpass("Authorization（隐藏输入，不会打印）: ").strip()
            if not token or "\n" in token or "\r" in token:
                raise ConfigError("Authorization 为空或包含换行")
            with exclusive(settings.data_dir):
                save_authorization(settings, token)
            emit({"action": "authorization_saved", "mode": "0600"})
            return 0
        if args.command == "schedule":
            emit(render_schedule(settings, args))
            return 0
        if args.command == "vehicles":
            settings.validate_live()
            response, relogin = with_relogin(settings, lambda: ApiClient(settings).state())
            result = {"action": "vehicles", **sync_vehicles(settings, response, create=args.create)}
            if relogin:
                result["relogin"] = relogin
            result["checked_at"] = datetime.now(BEIJING).isoformat(timespec="seconds")
            emit(result)
            return 2 if result["duplicates"] else 0
        if args.command in {"history", "resolve"}:
            with exclusive(settings.data_dir), Journal(settings.data_dir) as journal:
                if args.command == "history":
                    rows = [{k: v for k, v in row.items() if k != "vehicle"}
                            for row in journal.history(settings.plate)]
                    emit({"action": "history", "attempts": rows})
                else:
                    journal.allow_retry(args.attempt, settings.plate)
                    emit({"action": "protection_released", "attempt": args.attempt})
            return 0
        now = datetime.now(BEIJING)
        permit = getattr(args, "permit", None) or settings.default_permit
        relogin = None
        others = _same_vehicle_configs(settings) if args.command == "run" else []
        if args.command == "run" and args.scheduled and not settings.auto_renew:
            # On-demand vehicles are handled only when the user names them.
            result = {"action": "wait", "reason": "这辆车设为按需办理，定时任务不处理",
                      "vehicle": mask_plate(settings.plate), "permit": permit}
        elif others:
            # Each config keeps its own journal, so two of them could submit the same day twice.
            result = {"action": "blocked", "vehicle": mask_plate(settings.plate), "permit": permit,
                      "reason": "同一辆车还有其他配置：" + "、".join(others) + "；提交记录互不相通，请只保留一份"}
        elif args.command == "run":
            # A credential rejected by pre-submit checks is reported before anything
            # is journaled, so running again after a login cannot submit twice.
            result, relogin = with_relogin(
                settings, lambda: execute(settings, permit, submit=args.submit),
                lambda result: result.get("category") == "auth_expired" and "attempt" not in result)
            now = datetime.now(BEIJING)
        else:
            fixture = getattr(args, "fixture", None)
            simulated = getattr(args, "at", None)
            if simulated and not fixture:
                raise ConfigError("--at 仅允许与离线 --fixture 一起使用")
            if simulated:
                now = datetime.fromisoformat(simulated)
                now = now.replace(tzinfo=BEIJING) if now.tzinfo is None else now.astimezone(BEIJING)
            if fixture:
                if fixture.stat().st_size > 5 * 1024 * 1024:
                    raise ConfigError("离线响应样本过大")
                response = json.loads(fixture.read_text(encoding="utf-8"))
            else:
                settings.validate_live()
                response, relogin = with_relogin(settings, lambda: ApiClient(settings).state())
                now = datetime.now(BEIJING)
            records = normalize(response, settings.plate)
            if args.command == "status":
                vehicle = select_vehicle(response, settings.plate)
                remaining = vehicle.get("sycs")
                # Only explicit safe fields, never permit IDs, names, credentials or raw messages.
                result = {"action": "status", "vehicle": mask_plate(settings.plate), "records": [
                    {"permit": record_type(row), "status": row.get("blztmc"),
                     "start": row.get("yxqs"), "end": row.get("yxqz")} for row in records],
                    "can_apply": _can_apply(vehicle), "inside_remaining": _remaining_count(remaining)}
            else:
                result = describe(plan(records, permit, now, settings.earliest_time,
                                       settings.outside_lead_days), settings.plate)
                if fixture:
                    result["offline"] = True
        if relogin:
            result["relogin"] = relogin
        result["checked_at"] = now.isoformat(timespec="seconds")
        emit(result)
        return 2 if result["action"] in {"blocked", "uncertain", "rejected"} else 0
    except (ConfigError, DomainError, ApiError, AuthError, BusyError, ValueError, OSError, sqlite3.Error):
        # Controlled exceptions carry safe messages; arbitrary OS errors may include private paths.
        error = sys.exception()
        message = str(error) if isinstance(error, (ConfigError, DomainError, ApiError, AuthError, BusyError)) else "本地文件或参数处理失败，请检查路径、权限和格式"
        result = {"action": "error", "reason": message}
        if isinstance(error, AuthError):
            result.update({"category": error.category, "business_auth_verified": False})
        elif isinstance(error, ApiError) and error.auth_expired:
            result["category"] = "auth_expired"
            if isinstance(error, ReloginFailed):
                result["relogin"] = "failed"
        emit(result)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
