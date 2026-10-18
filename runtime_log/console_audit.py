"""Operator console audit log (rt_control_arm_console --audit-log, JSON lines) -> timeline events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from .model import Event

OPERATOR = "operator"
LABELS = {
    "login": "操作台登录",
    "acquire": "取得控制权",
    "release": "释放控制权",
    "preview": "预览目标",
    "execute": "执行目标",
    "stop": "操作台停止",
    "goto_pose": "去位姿",
    "run_sequence": "运行位姿序列",
    "save_pose": "保存位姿",
    "delete_pose": "删除位姿",
}


def label(entry: dict) -> str:
    action = entry.get("action", "")
    if action == "enable":
        return "操作台使能" if entry.get("args", {}).get("enabled") else "操作台失能"
    return LABELS.get(action, action)


def summary(entry: dict) -> str:
    args = entry.get("args", {})
    parts = []
    if "name" in args:
        parts.append(str(args["name"]))
    if "names" in args:
        parts.append(" → ".join(map(str, args["names"]))[:120])
    if "gear" in args:
        parts.append(f"档位 {args['gear']}")
    if "target" in args and isinstance(args["target"], (list, dict)):
        values = args["target"].values() if isinstance(args["target"], dict) else args["target"]
        parts.append("目标 [" + ", ".join(f"{float(v):.3f}" for v in values) + "]")
    if not entry.get("ok"):
        parts.append(f"被拒绝：{entry.get('message') or entry.get('error', '')}")
    who = f"会话 {entry['session']}" if entry.get("session") else ""
    client = entry.get("client", "")
    tail = " ".join(x for x in (who, client) if x)
    return "；".join(parts) + (f"（{tail}）" if tail else "")


def to_event(entry: dict) -> Optional[Event]:
    try:
        ts = float(entry["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    action = entry.get("action", "?")
    return Event(
        ts, OPERATOR, "console", f"pid {entry.get('pid', '')}", summary(entry), "info" if entry.get("ok") else "warn",
        rule=f"console_{action}", label=label(entry), data={"action": action, "ok": bool(entry.get("ok")), "domain": str(entry.get("domain", ""))},
    )


def parse_lines(lines: Iterable[str], domain: str, start: float, end: Optional[float] = None) -> list[Event]:
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if str(entry.get("domain", "")) != str(domain):
            continue
        event = to_event(entry)
        if event is None or event.ts < start or (end is not None and event.ts > end):
            continue
        events.append(event)
    return events


def default_path(home: Optional[str]) -> Path:
    return Path(home or Path.home()) / ".ros/log/rt_control_arm_console/audit.jsonl"
