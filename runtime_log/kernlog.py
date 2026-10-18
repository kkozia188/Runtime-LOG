"""IgH EtherCAT master kernel messages from `journalctl -k -o json` (generic, not project specific)."""

from __future__ import annotations

import json
import re
from typing import Iterable, Iterator, Optional

from .model import ECAT, Event

# (rule id, regex, level, label); first match wins.
RULES: list[tuple[str, re.Pattern, str, str]] = [
    ("master_requested", re.compile(r"^EtherCAT: Successfully requested master (?P<master>\d+)"), "info", "应用占用主站"),
    ("master_requesting", re.compile(r"^EtherCAT: Requesting master (?P<master>\d+)"), "info", "请求主站"),
    ("master_releasing", re.compile(r"^EtherCAT (?P<master>\d+): Releasing master"), "info", "释放主站"),
    ("master_released", re.compile(r"^EtherCAT (?P<master>\d+): Released\."), "info", "主站已释放"),
    ("responding", re.compile(r"^EtherCAT (?P<master>\d+): (?P<count>\d+) slave\(s\) responding on (?P<device>\w+) device"), "warn", "应答从站数变化"),
    ("slave_states", re.compile(r"^EtherCAT (?P<master>\d+): Slave states on (?P<device>\w+) device: (?P<states>.+?)\.?$"), "info", "从站状态"),
    ("rescan_now", re.compile(r"^EtherCAT (?P<master>\d+): Re-scanning now"), "warn", "重新扫描"),
    ("scan_start", re.compile(r"^EtherCAT (?P<master>\d+): Scanning bus"), "info", "开始扫描总线"),
    ("scan_done", re.compile(r"^EtherCAT (?P<master>\d+): Bus scanning completed in (?P<ms>\d+) ms"), "info", "总线扫描完成"),
    ("dc_reference", re.compile(r"^EtherCAT (?P<master>\d+): Using slave (?P<slave>\d+) as DC reference clock"), "info", "DC 参考时钟"),
    ("link_down", re.compile(r"^EtherCAT (?P<master>\d+): Link state of (?P<dev>\S+) changed to (?P<state>DOWN)"), "warn", "网口链路断开"),
    ("link_up", re.compile(r"^EtherCAT (?P<master>\d+): Link state of (?P<dev>\S+) changed to (?P<state>\w+)"), "info", "网口链路恢复"),
    ("domain", re.compile(r"^EtherCAT (?P<master>\d+): Domain(?P<domain>\d+): Logical address \S+, (?P<bytes>\d+) byte, expected working counter (?P<wkc>\d+)"), "info", "过程数据域"),
    ("op_thread", re.compile(r"^EtherCAT (?P<master>\d+): Starting EtherCAT-OP thread"), "info", "主站进入运行线程"),
    ("idle_thread", re.compile(r"^EtherCAT (?P<master>\d+): Starting EtherCAT-IDLE thread"), "info", "主站回到空闲线程"),
    ("not_synced", re.compile(r"^EtherCAT WARNING (?P<master>\d+)-(?P<slave>\d+): Slave did not sync after (?P<ms>\d+) ms"), "warn", "从站 DC 未同步（超时放行）"),
    ("datagrams_timed_out", re.compile(r"^EtherCAT WARNING (?P<master>\d+): (?P<count>\d+) datagrams? TIMED OUT"), "warn", "报文超时"),
    ("datagrams_unmatched", re.compile(r"^EtherCAT WARNING (?P<master>\d+): (?P<count>\d+) datagrams? UNMATCHED"), "warn", "报文不匹配"),
    ("al_state_timeout", re.compile(r"^EtherCAT ERROR (?P<master>\d+)-(?P<slave>\d+): Failed to receive AL state datagram"), "error", "AL 状态报文超时"),
    ("dc_check_failed", re.compile(r"^EtherCAT ERROR (?P<master>\d+)-(?P<slave>\d+): Failed to check DC synchrony"), "error", "DC 同步检查失败"),
    ("pdo_assign_read_failed", re.compile(r"^EtherCAT ERROR (?P<master>\d+)-(?P<slave>\d+): Failed to read number of assigned PDOs for SM(?P<sm>\d+)"), "error", "读 PDO 分配失败（扫描中）"),
    ("sdo_request_failed", re.compile(r"^EtherCAT ERROR (?P<master>\d+)-(?P<slave>\d+): Failed to process SDO request"), "error", "SDO 请求失败（外部工具或应用读写对象字典）"),
    ("al_status", re.compile(r'^EtherCAT ERROR (?P<master>\d+)-(?P<slave>\d+): AL status message (?P<code>0x[0-9A-Fa-f]+): "(?P<text>[^"]*)"'), "error", "从站 AL 状态错误"),
    ("safeop_error", re.compile(r"^EtherCAT WARNING (?P<master>\d+)-(?P<slave>\d+): Changed to (?P<state>\S+) \+ ERROR in the meantime"), "warn", "从站自行进入错误状态"),
    ("slave_error", re.compile(r"^EtherCAT ERROR (?P<master>\d+)-(?P<slave>\d+): (?P<detail>.+)"), "error", "从站错误"),
    ("master_error", re.compile(r"^EtherCAT ERROR (?P<master>\d+): (?P<detail>.+)"), "error", "主站错误"),
    ("slave_warning", re.compile(r"^EtherCAT WARNING (?P<master>\d+)-(?P<slave>\d+): (?P<detail>.+)"), "warn", "从站警告"),
    ("master_warning", re.compile(r"^EtherCAT WARNING (?P<master>\d+): (?P<detail>.+)"), "warn", "主站警告"),
]

# Rules counted in the per-run anomaly summary.
ANOMALY_RULES = (
    "responding",
    "rescan_now",
    "link_down",
    "pdo_assign_read_failed",
    "sdo_request_failed",
    "al_status",
    "safeop_error",
    "not_synced",
    "datagrams_timed_out",
    "datagrams_unmatched",
    "al_state_timeout",
    "dc_check_failed",
    "slave_error",
    "master_error",
)

# Messages without diagnostic value in a timeline (thread binding, datagram layout lines, ...).
IGNORED = (
    re.compile(r"binding thread to cpu"),
    re.compile(r"Datagram domain\d+-\d+-main"),
    re.compile(r"Master thread exited"),
    re.compile(r"Re-scanning on next possibility") ,
)


def classify(message: str) -> Optional[tuple[str, str, str, dict]]:
    for rule, pattern, level, label in RULES:
        match = pattern.search(message)
        if match:
            return rule, level, label, {k: _number(v) for k, v in match.groupdict().items() if v is not None}
    return None


def _number(value: str):
    try:
        return int(value)
    except ValueError:
        return value


def parse_entry(entry: dict) -> Optional[Event]:
    """One journal JSON entry -> Event; None for non-EtherCAT or ignored lines."""
    message = entry.get("MESSAGE")
    if isinstance(message, list):  # journald stores non-UTF-8 text as a byte array
        message = bytes(message).decode("utf-8", "replace")
    if not isinstance(message, str) or "EtherCAT" not in message:
        return None
    message = message.strip()
    classified = classify(message)
    if classified is None:
        if any(pattern.search(message) for pattern in IGNORED):
            return None
        rule, level, label, data = "other", "info", "EtherCAT", {}
    else:
        rule, level, label, data = classified
        # "N slave(s) responding ... Re-scanning on next possibility" is the count line we keep.
    subject = f"slave {data['slave']}" if "slave" in data and rule != "dc_reference" else f"master {data.get('master', '')}".strip()
    ts = int(entry["__REALTIME_TIMESTAMP"]) / 1e6
    mono = int(entry["_SOURCE_MONOTONIC_TIMESTAMP"]) / 1e6 if "_SOURCE_MONOTONIC_TIMESTAMP" in entry else None
    return Event(ts, ECAT, "kernel", subject, message, level, rule=rule, label=label, data=data, mono=mono)


def reclassify(event: Event) -> Optional[Event]:
    """Apply the current rules to an archived kernel event (replay)."""
    entry = {"MESSAGE": event.message, "__REALTIME_TIMESTAMP": str(int(round(event.ts * 1e6)))}
    if event.mono is not None:
        entry["_SOURCE_MONOTONIC_TIMESTAMP"] = str(int(round(event.mono * 1e6)))
    return parse_entry(entry)


def parse_lines(lines: Iterable[str]) -> Iterator[Event]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = parse_entry(entry)
        if event is not None:
            yield event
