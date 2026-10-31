"""Query -> decide -> journal -> submit once -> verify by querying again."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from .api import (ApiClient, ApiError, COUNT_CHECK_PATH, DATES_PATH, DRIVER_PATH,
                  ROUTE_PATH, SITE_CONFIG_PATH, VEHICLE_CHECK_PATH)
from .config import ConfigError, IN_BEIJING_FIELDS, ROAD_FIELDS
from .domain import EXPIRED, DomainError, normalize, plan, record_type, select_vehicle
from .storage import Journal, attempt_key, exclusive


# The official home page passes exactly these vehicle fields into the apply pages.
VEHICLE_FIELDS = ("vId", "hphm", "hpzl", "ylzsfkb", "elzsfkb", "elzqyms", "ylzqyms",
                  "elzmc", "ylzmc", "cllx")
AVAILABLE_FLAG = {"inside": "ylzsfkb", "outside": "elzsfkb"}
BEIJING = ZoneInfo("Asia/Shanghai")


def mask_plate(plate):
    return plate[:2] + "***" + plate[-2:] if len(plate) >= 5 else "***"


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def prepare_submission(settings, decision, vehicle, client):
    """Mirror the official apply pages; every failed server check stops before a journal entry."""
    settings.validate_application(decision.permit)
    if any(not _text(vehicle.get(key)) for key in ("vId", "hphm", "hpzl")):
        raise DomainError("车辆列表缺少办理所需的车辆资料，请先在 App 核对")
    if vehicle["hphm"].strip().upper() != settings.plate.strip().upper():
        raise DomainError("车辆列表与配置车牌不一致")
    if vehicle.get(AVAILABLE_FLAG[decision.permit]) is not True:
        raise DomainError("服务端显示该车辆当前不可办理此类进京证")
    jjzzl = "01" if decision.permit == "inside" else "02"
    vehicle_id = {"vId": vehicle["vId"], "jjzzl": jjzzl, "hphm": vehicle["hphm"]}
    checked = client.check(VEHICLE_CHECK_PATH, {"hphm": vehicle["hphm"], "hpzl": vehicle["hpzl"]})
    if checked.get("data") not in (200, "200"):
        raise DomainError("服务端车辆办证校验未通过，请在 App 核对车辆信息")
    driver = client.check(DRIVER_PATH, {}).get("data")
    if (not isinstance(driver, dict) or not _text(driver.get("jsrxm")) or not _text(driver.get("jszh"))
            or not isinstance(driver.get("dabh"), (str, type(None)))):
        raise DomainError("服务端未返回完整的驾驶人信息，请在 App 核对")
    site = client.check(SITE_CONFIG_PATH, {}).get("data")
    if not isinstance(site, dict) or site.get("txrkg") not in ("0", 0):
        # With companion registration on, the official page's choices are not reproducible here.
        raise DomainError("服务端已开启同行人登记，请在 App 办理并核对后再启用自动续办")
    application = settings.application_for(decision.permit)
    # rylx is the official page's "港澳台及外籍人士" option; always applied as "0" (not selected).
    people = {"jsrxm": driver["jsrxm"], "jszh": driver["jszh"], "dabh": driver.get("dabh"),
              "rylx": "0", "txrxx": list(application["txrxx"])}
    client.check(COUNT_CHECK_PATH, {**people, "txrkg": site["txrkg"], "wtxr": ""})
    dates = client.check(DATES_PATH, vehicle_id).get("data")
    allowed = dates.get("jjrqs") if isinstance(dates, dict) else None
    if not isinstance(allowed, list) or decision.apply_date.isoformat() not in allowed:
        raise DomainError("服务端可选进京日期不含计划日期，暂不提交")
    if client.check(ROUTE_PATH, {"vId": vehicle["vId"]}).get("data") not in ("0", 0):
        raise DomainError("服务端要求填写行驶路线，程序不支持，请在 App 办理")
    payload = {key: vehicle.get(key) for key in VEHICLE_FIELDS}
    payload.update({"jjzzl": jjzzl, **people, "jjrq": decision.apply_date.isoformat(),
                    "jingState": ""})
    payload.update({key: value for key, value in application.items() if key != "txrxx"})
    unused = ROAD_FIELDS if application["sfzj"] == "1" else IN_BEIJING_FIELDS
    payload.update({key: "" for key in unused})
    return payload


def _complete_period(record):
    try:
        start = date.fromisoformat(record["yxqs"])
        end = date.fromisoformat(record["yxqz"])
    except (TypeError, ValueError, KeyError):
        return None
    if start > end or start.isoformat() != record["yxqs"] or end.isoformat() != record["yxqz"]:
        return None
    return start, end


def reconcile(journal, plate, records, now):
    today = now.astimezone(BEIJING).date()
    for attempt in journal.unresolved(plate):
        for record in records:
            if record_type(record) != attempt["permit"]:
                continue
            if record.get("yxqs") != attempt["apply_date"]:
                continue
            state = record.get("blztmc")
            if state in {"审核通过(生效中)", "审核通过(待生效)"}:
                journal.update(attempt["key"], "approved" if _complete_period(record) else "verified")
                break
            if state == "审核中":
                journal.update(attempt["key"], "verified")
                break
            if state in {"已取消", "失败(审核不通过)"}:
                journal.update(attempt["key"], "rejected")
                break
            if state in EXPIRED:
                period = _complete_period(record)
                # Only exact server evidence can finish an old intent. Never
                # infer expiry from elapsed local time or delete the attempt.
                if period and period[1] < today:
                    journal.update(attempt["key"], "expired")
                    break


def describe(decision, plate):
    result = {"action": decision.action, "reason": decision.reason,
              "vehicle": mask_plate(plate), "permit": decision.permit}
    if decision.apply_date:
        result["apply_date"] = decision.apply_date.isoformat()
    return result


def execute(settings, permit: str, now: datetime | None = None, submit=False, client=None, clock=None):
    """Even a submission flag cannot override missing/ambiguous server evidence."""
    settings.validate_live()
    client = client or ApiClient(settings)
    current_time = clock or (lambda: now if now is not None else datetime.now(BEIJING))
    with exclusive(settings.data_dir), Journal(settings.data_dir) as journal:
        response = client.state()
        records = normalize(response, settings.plate)
        queried_at = current_time()
        reconcile(journal, settings.plate, records, queried_at)
        # Network requests may cross noon or midnight; decide with the time after querying.
        decision = plan(records, permit, queried_at, settings.earliest_time, settings.outside_lead_days)
        result = describe(decision, settings.plate)
        if journal.unresolved(settings.plate):
            if decision.action == "wait":
                return result
            return {**result, "action": "blocked",
                    "reason": "上次申请结果尚未完整核验；先在官方 App 核验并查看 history"}
        if decision.action != "renew":
            return result
        key = attempt_key(settings.plate, permit, decision.apply_date.isoformat())
        if journal.get(key):
            return {**result, "action": "blocked", "attempt": key,
                    "reason": "相同车辆、类型和生效日已有提交记录，禁止重复提交"}
        try:
            payload = prepare_submission(settings, decision, select_vehicle(response, settings.plate), client)
        except (ConfigError, DomainError, ApiError) as error:
            blocked = {**result, "action": "blocked", "reason": "提交前校验未通过：" + str(error)}
            if isinstance(error, ApiError) and error.auth_expired:
                blocked["category"] = "auth_expired"
            return blocked
        checked_at = current_time()
        checked_decision = plan(records, permit, checked_at,
                                settings.earliest_time, settings.outside_lead_days)
        if (checked_decision.action != "renew" or checked_decision.apply_date != decision.apply_date
                or checked_at.astimezone(BEIJING).date() != queried_at.astimezone(BEIJING).date()):
            # Do not carry server checks or an idempotency key into a changed
            # date/window. The next run must query and validate the new plan.
            return {**describe(checked_decision, settings.plate), "action": "blocked",
                    "reason": "提交前办理时间或计划已变化，请重新运行并完成校验"}
        if not submit:
            return {**result, "action": "preview", "server_checks": "passed",
                    "reason": decision.reason + "；本次只预览"}
        if not settings.submission_enabled:
            return {**result, "action": "blocked", "reason": "配置尚未开启 submission_enabled"}
        key = journal.begin(settings.plate, permit, decision.apply_date.isoformat())
        try:
            client.submit(payload)
        except ApiError as error:
            state = "uncertain" if error.uncertain else "rejected"
            journal.update(key, state)
            return {**result, "action": state, "attempt": key, "reason": str(error)}
        journal.update(key, "accepted")
        try:
            refreshed = normalize(client.state(), settings.plate)
            reconcile(journal, settings.plate, refreshed, current_time())
        except (ApiError, DomainError):
            pass # keep accepted durable: query again next time, never resubmit blindly
        outcome = journal.get(key)["state"]
        reasons = {"accepted": "接口已受理，尚未查询到对应新证；保留记录等待核验",
                   "verified": "服务端已确认申请，审批结果或有效期仍待核验",
                   "approved": "服务端已返回审核通过的对应申请，请在 App 核对有效期",
                   "rejected": "服务端显示对应申请已取消或审核未通过，请在 App 核对原因",
                   "expired": "服务端已确认对应申请已过期，后续运行将重新评估续办"}
        return {**result, "action": "wait" if outcome == "expired" else outcome,
                "attempt": key, "reason": reasons[outcome]}
