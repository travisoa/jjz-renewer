"""Pure renewal decisions; no network, filesystem, or device side effects."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
ACTIVE = "审核通过(生效中)"
PENDING = {"审核中", "审核通过(待生效)"}
FAILED = {"已取消", "失败(审核不通过)"}
EXPIRED = {"已过期", "审核通过(已失效)", "审核通过(已过期)"}
KNOWN = {ACTIVE} | PENDING | FAILED | EXPIRED


class DomainError(ValueError):
    """The response cannot be used without guessing its meaning."""


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str
    permit: str
    apply_date: date | None = None
    record: dict | None = None


def _plate(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DomainError("车牌字段必须是文本")
    return value.strip().upper()


def _vehicle_list(response: dict) -> list:
    if not isinstance(response, dict) or not isinstance(response.get("data"), dict):
        raise DomainError("查询响应缺少 data 对象")
    vehicles = response["data"].get("bzclxx")
    if not isinstance(vehicles, list):
        raise DomainError("查询响应缺少车辆列表 bzclxx")
    return vehicles


def account_vehicles(response: dict) -> list[tuple[str, dict]]:
    """Return every bound vehicle with its plate; each plate must be present and distinct."""
    result: list[tuple[str, dict]] = []
    for vehicle in _vehicle_list(response):
        if not isinstance(vehicle, dict):
            raise DomainError("车辆列表中出现非法对象")
        plate = _plate(vehicle.get("hphm"))
        if not plate or any(ord(c) < 32 or ord(c) == 127 for c in plate):
            raise DomainError("车辆列表中有车辆缺少有效车牌，需在 App 核对")
        if any(plate == seen for seen, _ in result):
            raise DomainError("车辆列表中同一车牌出现多次，需在 App 核对")
        result.append((plate, vehicle))
    return result


def normalize(response: dict, plate: str) -> list[dict]:
    """Select one exact plate and combine both response record collections.

    Missing record arrays are accepted; malformed arrays and conflicting vehicle
    identities are not. A known vehicle with no records returns an empty list.
    """
    wanted = _plate(plate)
    if not wanted:
        raise DomainError("必须明确配置目标车牌")
    vehicles = _vehicle_list(response)
    found = False
    records: list[dict] = []
    for vehicle in vehicles:
        if not isinstance(vehicle, dict):
            raise DomainError("车辆列表中出现非法对象")
        parent_plate = _plate(vehicle.get("hphm"))
        found |= parent_plate == wanted
        for key in ("ecbzxx", "bzxx"):
            children = vehicle.get(key)
            if children is None:
                children = []
            if not isinstance(children, list):
                raise DomainError(f"证件字段 {key} 必须是列表或 null")
            for child in children:
                if not isinstance(child, dict):
                    raise DomainError("证件列表中出现非法对象")
                child_plate = _plate(child.get("hphm"))
                if parent_plate and child_plate and parent_plate != child_plate:
                    if wanted in (parent_plate, child_plate):
                        raise DomainError("父车辆与证件的车牌不一致，已停止处理")
                actual_plate = child_plate or parent_plate
                if actual_plate != wanted:
                    continue
                found = True
                parent_vid, child_vid = vehicle.get("vId"), child.get("vId")
                if parent_vid and child_vid and parent_vid != child_vid:
                    raise DomainError("父车辆与证件的车辆标识不一致")
                records.append({**child, "hphm": wanted})
    if not found:
        raise DomainError("接口中未找到目标车辆，请检查车牌及当前登录账号")
    vehicle_ids = {str(r["vId"]) for r in records if r.get("vId") not in (None, "")}
    if len(vehicle_ids) > 1:
        raise DomainError("同一车牌对应多个车辆标识，需在 App 核对")

    # The same application can appear in both lists. Never silently choose one
    # of two conflicting states or identities for that application.
    unique: list[dict] = []
    by_id: dict[str, dict] = {}
    protected = {
        "hphm", "vId", "hpzl", "jszh", "jjzzl", "jjzzlmc",
        "blztmc", "yxqs", "yxqz",
    }
    for record in records:
        application_id = record.get("applyId")
        if not application_id:
            unique.append(record)
            continue
        identity = str(application_id)
        previous = by_id.get(identity)
        if previous is None:
            by_id[identity] = record
            unique.append(record)
            continue
        for field in protected:
            old, new = previous.get(field), record.get(field)
            if old not in (None, "") and new not in (None, "") and old != new:
                raise DomainError("同一申请出现相互冲突的证件信息")
        previous.update({k: v for k, v in record.items() if v not in (None, "")})
    return unique


def select_vehicle(response: dict, plate: str) -> dict:
    """Return the vehicle object the official apply button starts from.

    Call after normalize(), which has already validated the response shape.
    """
    wanted = _plate(plate)
    matches = [vehicle for vehicle in response["data"]["bzclxx"]
               if _plate(vehicle.get("hphm")) == wanted]
    if len(matches) != 1:
        raise DomainError("目标车牌在车辆列表中不唯一或缺失，需在 App 核对")
    return matches[0]


def record_type(record: dict) -> str:
    if not isinstance(record, dict):
        raise DomainError("证件记录必须是对象")
    code, label = record.get("jjzzl"), record.get("jjzzlmc")
    if code is not None and not isinstance(code, str):
        raise DomainError("证件类型编码格式错误")
    by_code = {"01": "inside", "02": "outside"}.get(code)
    by_label = None
    if isinstance(label, str):
        if "六环内" in label and "六环外" in label:
            raise DomainError("证件类型名称同时包含六环内和六环外")
        if "六环内" in label:
            by_label = "inside"
        elif "六环外" in label:
            by_label = "outside"
    if by_code and by_label and by_code != by_label:
        raise DomainError("证件类型编码与名称不一致")
    result = by_code or by_label
    if result is None:
        raise DomainError("无法识别证件类型，需核对接口响应")
    return result


def _day(record: dict, field: str, required: bool = True) -> date | None:
    value = record.get(field)
    if value in (None, ""):
        if required:
            raise DomainError("证件缺少有效期，不能推算办理日期")
        return None
    if not isinstance(value, str):
        raise DomainError("证件有效期格式错误")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("证件有效期格式错误") from exc
    if parsed.isoformat() != value:
        raise DomainError("证件有效期须为 YYYY-MM-DD")
    return parsed


def _applied_at(record: dict) -> datetime | None:
    value = record.get("sqsj")
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise DomainError("申请时间格式错误，无法确认最新记录")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError("申请时间格式错误，无法确认最新记录") from exc
    if result.tzinfo is None:
        result = result.replace(tzinfo=SHANGHAI)
    return result.astimezone(SHANGHAI)


def _period(record: dict) -> tuple[date, date]:
    start, end = _day(record, "yxqs"), _day(record, "yxqz")
    if start > end:
        raise DomainError("证件有效期起止顺序错误")
    return start, end


def _latest(records: list[dict]) -> dict:
    """Use dates, never server array order, including canceled applications."""
    ranked = []
    for record in records:
        start = _day(record, "yxqs", required=False)
        applied = _applied_at(record)
        if start is None and applied is None:
            raise DomainError("证件缺少有效期和申请时间，无法确定最新记录")
        # A later failed attempt must not be hidden by a prior successful permit.
        anchor = max(d for d in (start, applied.date() if applied else None) if d)
        ranked.append((anchor, applied or datetime.min.replace(tzinfo=SHANGHAI), record))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(ranked) > 1 and ranked[0][:2] == ranked[1][:2]:
        first, second = ranked[0][2], ranked[1][2]
        if any(first.get(k) != second.get(k) for k in ("blztmc", "yxqs", "yxqz")):
            raise DomainError("同时间存在相互冲突的证件记录，需人工核对")
    return ranked[0][2]


def plan(
    records: list[dict], permit: str, now: datetime,
    earliest_time: str = "08:00", outside_lead_days: int = 0,
) -> Decision:
    """Return a conservative, side-effect-free renewal decision."""
    if permit not in ("outside", "inside"):
        raise DomainError("证件类型必须为 outside 或 inside")
    if now.tzinfo is None or now.utcoffset() is None:
        raise DomainError("当前时间必须带时区")
    if not isinstance(outside_lead_days, int) or not 0 <= outside_lead_days <= 1:
        raise DomainError("六环外提前天数只能为 0 或 1")
    try:
        earliest = time.fromisoformat(earliest_time)
    except (TypeError, ValueError) as exc:
        raise DomainError("最早办理时间格式应为 HH:MM") from exc
    if earliest.tzinfo is not None:
        raise DomainError("最早办理时间使用北京时间，不应带时区")
    now = now.astimezone(SHANGHAI)
    today = now.date()
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise DomainError("证件记录必须是对象列表")

    # A pending application of either type prevents another submission, even
    # when the pending record has no validity dates yet.
    if any(isinstance(r.get("blztmc"), str) and r["blztmc"] in PENDING for r in records):
        return Decision("wait", "已有审核中或待生效申请，等待状态更新", permit)
    if not records:
        return Decision("blocked", "尚无证件记录，请先在北京交警 App 完成首次办理", permit)

    try:
        types = [(r, record_type(r)) for r in records]
        if any(not isinstance(r.get("blztmc"), str) or r["blztmc"] not in KNOWN for r in records):
            raise DomainError("出现未知办理状态，已停止自动提交")
        active_periods = []
        for record, kind in types:
            if record.get("blztmc") == ACTIVE:
                start, end = _period(record)
                if start > today:
                    raise DomainError("生效中证件的起始日期在未来，需核对状态")
                active_periods.append((record, kind, start, end))

        if permit == "outside":
            live_inside = [p for p in active_periods if p[1] == "inside" and p[3] >= today]
            if any(p[3] > today for p in live_inside):
                return Decision("wait", "六环内证仍有效，暂不切换六环外证", permit)
        else:
            live_inside = []

        candidates = [r for r, kind in types if kind == permit]
        if candidates:
            latest = _latest(candidates)
            if latest.get("blztmc") in FAILED:
                return Decision("blocked", "最新申请已取消或审核未通过，请在 App 核对后再办理", permit, record=latest)
            start, end = _period(latest)
            if start > today:
                raise DomainError("最新证件的有效期在未来，需核对办理状态")
            due = end - timedelta(days=outside_lead_days if permit == "outside" else 0)
            if today < due:
                return Decision("wait", f"尚未到续办日（{due.isoformat()}）", permit, record=latest)
            reason = "已到续办时间，可申请接续证件"
        else:
            # Like the official type picker, switching only needs the server's
            # availability flag and date list, both checked before submission.
            # A live inside permit still delays outside until the day after it ends.
            latest, end = None, None
            name, other = ("六环内", "六环外") if permit == "inside" else ("六环外", "六环内")
            reason = f"无{name}证记录，可从{other}切换办理{name}证"
        if now.time() < earliest:
            return Decision("wait", f"等待北京时间 {earliest_time} 后办理", permit, record=latest)

        if end is not None and end >= today:
            apply_date = end + timedelta(days=1)
        elif permit == "inside" and now.time() >= time(12):
            apply_date = today + timedelta(days=1)
        else:
            apply_date = today
        if live_inside:
            apply_date = max(apply_date, max(p[3] for p in live_inside) + timedelta(days=1))
        live_application_ids = {
            ("known", str(record["applyId"])) if record.get("applyId") not in (None, "")
            else ("missing", index)
            for index, (record, _kind, active_start, active_end) in enumerate(active_periods)
            if active_start <= today <= active_end
        }
        if len(live_application_ids) > 1:
            raise DomainError("同日存在多张生效中证件，状态相互冲突，需先在 App 核对")
        return Decision("renew", reason, permit, apply_date, latest)
    except DomainError as exc:
        return Decision("blocked", str(exc), permit)
