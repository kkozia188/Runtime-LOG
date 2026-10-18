"""CiA 402 status word decoding (IEC 61800-7-201 / DS402 state machine)."""

from __future__ import annotations

# (mask, value, state) checked in order.
STATES = (
    (0x4F, 0x00, "not_ready_to_switch_on"),
    (0x4F, 0x40, "switch_on_disabled"),
    (0x6F, 0x21, "ready_to_switch_on"),
    (0x6F, 0x23, "switched_on"),
    (0x6F, 0x27, "operation_enabled"),
    (0x6F, 0x07, "quick_stop_active"),
    (0x4F, 0x0F, "fault_reaction_active"),
    (0x4F, 0x08, "fault"),
)
LABELS = {
    "not_ready_to_switch_on": "未就绪",
    "switch_on_disabled": "禁止合闸",
    "ready_to_switch_on": "合闸就绪",
    "switched_on": "已合闸",
    "operation_enabled": "运行使能",
    "quick_stop_active": "快速停机",
    "fault_reaction_active": "故障反应中",
    "fault": "故障",
    "unknown": "未知",
}
VOLTAGE_ENABLED = 1 << 4
WARNING = 1 << 7
TARGET_REACHED = 1 << 10
INTERNAL_LIMIT = 1 << 11
POWERED_STATES = ("switched_on", "operation_enabled")


def state(word: int) -> str:
    for mask, value, name in STATES:
        if word & mask == value:
            return name
    return "unknown"


def decode(word: int) -> dict:
    word = int(word) & 0xFFFF
    name = state(word)
    return {
        "word": f"0x{word:04X}",
        "state": name,
        "label": LABELS[name],
        "voltage_enabled": bool(word & VOLTAGE_ENABLED),
        "warning": bool(word & WARNING),
        "target_reached": bool(word & TARGET_REACHED),
        "internal_limit": bool(word & INTERNAL_LIMIT),
    }


def describe(word: int) -> str:
    info = decode(word)
    flags = [label for key, label in (("voltage_enabled", "动力电"), ("warning", "警告"), ("internal_limit", "内部限位")) if info[key]]
    return f"{info['label']} {info['word']}" + (f"（{'、'.join(flags)}）" if flags else "")
