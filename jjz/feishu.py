"""进京证办理状态的飞书 Card 2.0 卡片，规范见 .agents/skills/jjz-feishu-card/SKILL.md。

卡片只由 jjz 命令输出的 JSON 生成，不联网，车牌沿用其中的脱敏形式。send 经本机
lark-cli 用机器人私信本人；除此之外这里不发送任何消息。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
PERMITS = {"outside": "六环外证", "inside": "六环内证"}
# 结论类别 -> (header 主色, 标签文字, 标签色)
STYLES = {
    "approved": ("green", "已通过", "green"),
    "pending": ("blue", "审核中", "yellow"),
    "idle": ("grey", "无需办理", "neutral"),
    "ready": ("orange", "待提交", "orange"),
    "blocked": ("orange", "需核对", "orange"),
    "failed": ("red", "异常", "red"),
}
ACTIONS = {"wait": "等待", "preview": "可办理", "blocked": "已拦下", "accepted": "已受理",
           "verified": "已确认申请", "approved": "已通过", "uncertain": "结果不明",
           "rejected": "被拒绝", "error": "出错"}
NOT_SUBMITTED = {"ready": "提交开关未开", "idle": "无需提交", "pending": "已有申请",
                 "approved": "已有申请", "blocked": "已被拦下"}
RELOGIN = {"succeeded": "已自动重新登录并保存新凭证", "shared": "已使用同账号其他配置刚取得的新凭证",
           "failed": "自动重新登录失败"}
DEFAULT_TIP = "请到北京交警 App 确认办理结果，出行前以 App 显示为准。"


def load(path):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as file:
            lines = [line for line in file.read().splitlines() if line.strip()]
        data = json.loads(lines[-1])  # jjz 每条命令输出一行 JSON，取最后一行
    except (OSError, IndexError, ValueError):
        sys.exit(f"无法读取 jjz 输出：{path}")
    if not isinstance(data, dict):
        sys.exit(f"jjz 输出不是 JSON 对象：{path}")
    return data


def reason_of(result):
    text = str((result or {}).get("reason") or "")
    return text.removesuffix("；本次只预览").rstrip("。")


def day(value, today):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed.strftime("%m-%d") if parsed.year == today.year else parsed.isoformat()


def latest(status, permit):
    records = [r for r in (status or {}).get("records") or [] if r.get("permit") == permit]
    return max(records, key=lambda r: r.get("start") or "", default=None)


def period(record, today):
    start = day((record or {}).get("start"), today)
    if not start:
        return "—"
    end = day(record.get("end"), today)
    return f"{start} 至 {end}" if end else f"{start} 起 · 结束日期待定"


def conclude(mode, status, run, submit, permit, today):
    """返回 (结论类别, 一句话结论, 温馨提示)。"""
    name = PERMITS[permit]
    final = submit or run or status or {}
    action, reason = final.get("action"), reason_of(final)
    apply_date = day(final.get("apply_date") or (run or {}).get("apply_date"), today)
    record = latest(status, permit)
    state = str((record or {}).get("status") or "")
    if any((r or {}).get("category") == "auth_expired" for r in (status, run, submit)):
        headline = "凭证已失效，本次未办理" if mode == "apply" else "凭证已失效，本次未能检查"
        return "failed", headline, "凭证已失效，请在终端运行 auth-set --login 重新登录。"
    if action == "approved" or (action == "wait" and "待生效" in reason and state.startswith("审核通过")):
        if record and state.startswith("审核通过"):
            return "approved", f"{name}已审核通过，{period(record, today)}", DEFAULT_TIP
        return "approved", f"{name}已审核通过，{apply_date or '有效期待核对'} 起生效", DEFAULT_TIP
    if action in ("accepted", "verified") or (action == "wait" and "审核中或待生效" in reason):
        start = day((record or {}).get("start"), today) or apply_date
        headline = f"已提交 {start} 起的{name}申请，等待审核" if start else f"{name}申请已提交，等待审核"
        return "pending", headline, DEFAULT_TIP
    if action == "wait":
        return "idle", f"暂不需要办理{name}", f"{reason or '暂不需要办理'}。"
    if action == "preview":
        if mode == "check":
            return "ready", f"{name}尚未提交，现在仍可办理", "办理任务未提交成功，现在仍可办理，请本人决定是否办理。"
        return "ready", f"已可办理 {apply_date} 起的{name}，提交开关未开", "已可办理，提交开关未开，请本人决定是否办理。"
    if action == "blocked":
        tip = f"已被拦下：{reason}" + ("" if "App" in reason else "，请到 App 核对")
        return "blocked", "办理被拦下，本次未提交", tip + "。"
    if action == "uncertain":
        return "failed", "提交结果不明", "提交结果不明，请到 App 核实是否已有申请，切勿重复提交。"
    if action == "rejected":
        return "failed", "服务端拒绝了申请", "服务端拒绝了申请，请到 App 查看原因。"
    reason = reason or "结果未知"
    return "failed", f"办理出错：{reason}", f"程序出错：{reason}，请到 App 核对办理情况。"


def permit_cell(status, submit, permit, primary, today):
    """返回证件列的 (状态, 日期)；刚提交但查询里还没有新记录时，主证按提交结果显示。"""
    if not isinstance((status or {}).get("records"), list):
        return "未查询到", "—"
    record = latest(status, permit)
    apply_date = (submit or {}).get("apply_date")
    if primary and submit and submit.get("action") in ("accepted", "verified", "approved") \
            and (not record or record.get("start") != apply_date):
        label = {"accepted": "已受理", "verified": "审核中", "approved": "审核通过"}[submit["action"]]
        return label, f"{day(apply_date, today) or '—'} 起"
    if not record:
        return "无记录", "—"
    return record.get("status") or "状态未知", period(record, today)


def facts(mode, kind, status, run, submit, permit, now):
    today = now.date()
    final = submit or run or status or {}
    can = (status or {}).get("can_apply")
    remaining = (status or {}).get("inside_remaining")
    if mode == "check":
        submitted = "未提交（只检查）"
    elif submit:
        submitted = "已提交 · " + ACTIONS.get(submit.get("action"), str(submit.get("action")))
        if submit.get("attempt"):
            submitted += " · " + str(submit["attempt"])[:12]
    else:
        submitted = "未提交 · " + NOT_SUBMITTED.get(kind, "未执行")
    mark = lambda value: "可办" if value else "不可办"
    result = " · ".join(x for x in (ACTIONS.get(final.get("action"), ""), reason_of(final)) if x) or "—"
    if final.get("action") == "wait" and kind == "approved":
        state = str((latest(status, permit) or {}).get("status") or "")
        result = "已通过 · " + ("新证待生效" if "待生效" in state else "新证生效中")
    elif final.get("action") == "wait" and kind == "pending":
        result = "审核中 · 等待审批结果"
    relogin = next((r["relogin"] for r in (submit, run, status) if r and r.get("relogin")), None)
    return {
        "result": result,
        "submitted": submitted,
        "can": f"外 {mark(can.get('outside'))} · 内 {mark(can.get('inside'))}" if isinstance(can, dict) else "—",
        "remaining": f"{remaining} 次" if isinstance(remaining, int) else "—",
        "cells": {p: permit_cell(status, submit, p, p == permit, today) for p in PERMITS},
        "relogin": RELOGIN.get(relogin, f"自动重新登录：{relogin}") if relogin else None,
    }


def checked_time(*results):
    for result in results:
        try:
            return datetime.fromisoformat(result["checked_at"]).astimezone(BEIJING)
        except (TypeError, KeyError, ValueError):
            continue
    return datetime.now(BEIJING)


def build(mode, status, run, submit, permit, title):
    now = checked_time(submit, run, status)
    kind, headline, tip = conclude(mode, status, run, submit, permit, now.date())
    info = facts(mode, kind, status, run, submit, permit, now)
    vehicle = next((r["vehicle"] for r in (submit, run, status) if r and r.get("vehicle")), "车辆未知")
    subtitle = f"{now:%m-%d %H:%M} {'办理' if mode == 'apply' else '检查'} · {vehicle}"
    return kind, headline, tip, info, title or ("进京证办理" if mode == "apply" else "进京证办理检查"), subtitle


def card(mode, status, run, submit, permit, title=None):
    kind, headline, tip, info, title, subtitle = build(mode, status, run, submit, permit, title)
    main, tag, tag_color = STYLES[kind]
    other = next(p for p in PERMITS if p != permit)

    def column(p, primary):
        state, dates = info["cells"][p]
        size = {"text_size": "heading"} if primary else {}
        return {"tag": "column", "width": "weighted", "weight": 1,
                "background_style": f"{main}-50" if primary else "grey-50",
                "padding": "12px", "vertical_spacing": "2px", "elements": [
                    {"tag": "markdown", "content": f"<font color='grey'>{PERMITS[p]}</font>", "text_size": "notation"},
                    {"tag": "markdown", "content": f"**{state}**", **size},
                    {"tag": "markdown", "content": f"<font color='grey'>{dates}</font>", "text_size": "notation"}]}

    def field(name, value):
        return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{name}**\n{value}"}}

    tip_elements = [{"tag": "markdown", "content": "**温馨提示**"}, {"tag": "markdown", "content": tip}]
    elements = [
        {"tag": "markdown", "content": f"**{headline}**", "margin": "0px 0px 8px 0px"},
        {"tag": "column_set", "flex_mode": "bisect", "horizontal_spacing": "12px", "margin": "0px 0px 12px 0px",
         "columns": [column(permit, True), column(other, False)]},
        {"tag": "div", "margin": "0px 0px 12px 0px", "fields": [
            field("办理结果" if mode == "apply" else "核验结果", info["result"]),
            field("本次提交", info["submitted"]),
            field("可办状态", info["can"]),
            field("六环内剩余", info["remaining"])]},
    ]
    if info["relogin"]:
        elements.append({"tag": "markdown", "text_size": "notation",
                         "content": f"<font color='grey'>{info['relogin']}</font>"})
    elements.append({"tag": "column_set", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "background_style": "grey-50",
         "padding": "12px", "vertical_spacing": "4px", "elements": tip_elements}]})
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default", "summary": {"content": headline}},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": main,
            "icon": {"tag": "standard_icon", "token": "approval_colorful"},
            "text_tag_list": [{"tag": "text_tag", "text": {"tag": "plain_text", "content": tag}, "color": tag_color}],
        },
        "body": {"direction": "vertical", "padding": "12px 12px 20px 12px", "elements": elements},
    }


def text(mode, status, run, submit, permit, title=None):
    """卡片发送失败时的纯文本兜底，不超过 8 行。"""
    kind, headline, tip, info, title, subtitle = build(mode, status, run, submit, permit, title)
    lines = [f"【{title}·{STYLES[kind][1]}】{headline}（{subtitle}）"]
    lines += [f"{PERMITS[p]}：{state} {dates}" for p, (state, dates) in info["cells"].items()]
    lines += [f"{'办理结果' if mode == 'apply' else '核验结果'}：{info['result']}",
              f"本次提交：{info['submitted']}",
              f"可办状态：{info['can']}；六环内剩余 {info['remaining']}"]
    if info["relogin"]:
        lines.append(info["relogin"])
    lines.append(f"温馨提示：{tip}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="生成进京证办理状态的飞书卡片 JSON")
    parser.add_argument("--mode", choices=("apply", "check"), required=True, help="apply=办理任务，check=只检查")
    parser.add_argument("--status", required=True, help="jjz status 的输出文件")
    parser.add_argument("--run", help="jjz run 的输出文件（status 已失败时可省略）")
    parser.add_argument("--submit", help="jjz run --submit 的输出文件（本次提交过才传）")
    parser.add_argument("--permit", choices=tuple(PERMITS), help="本次办理的证件类型，默认取 run 输出，没有时为 outside")
    parser.add_argument("--format", choices=("card", "text"), default="card")
    parser.add_argument("--title", help="覆盖默认卡片标题")
    args = parser.parse_args()
    status, run, submit = load(args.status), load(args.run), load(args.submit)
    permit = args.permit or (run or {}).get("permit") or "outside"
    if args.format == "text":
        print(text(args.mode, status, run, submit, permit, args.title))
    else:
        print(json.dumps(card(args.mode, status, run, submit, permit, args.title), ensure_ascii=False))



def send(open_id, content, fallback, key):
    """Send the card, falling back to plain text; returns (outcome, reason) and never raises.

    Messages go only through the local lark-cli as the bot. The idempotency key keeps a
    retried send from arriving twice; its output is never echoed, since it carries IDs.
    """
    cli = shutil.which("lark-cli")
    if not cli:
        return "failed", "未找到 lark-cli"
    base = [cli, "im", "+messages-send", "--as", "bot", "--user-id", open_id]
    attempts = ((["--msg-type", "interactive", "--content", "-"], content, "", "sent"),
                (["--text", "-"], fallback, "-text", "sent_text"))
    for flags, body, suffix, outcome in attempts:
        try:
            done = subprocess.run([*base, *flags, "--idempotency-key", key + suffix], input=body,
                                  capture_output=True, text=True, timeout=60)
            if done.returncode == 0 and json.loads(done.stdout).get("ok") is True:
                return outcome, None
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            continue
    return "failed", "lark-cli 发送失败"
