"""Black box: keep the last 30 s of 1 kHz data from a recorder segment, save windows around triggers.

One thread per shared-memory segment, on non-isolated CPUs. It only reads the segment.
Windows are saved as MCAP (JSON messages, zstd) with the trigger reasons and the kernel log lines.
"""

from __future__ import annotations

import bisect
import collections
import json
import logging
import queue
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .shm import ShmReader

LOG = logging.getLogger("runtime_log.blackbox")
F64 = struct.Struct("<d")
SLOT_HEAD = struct.Struct("<Qqq")

# Status word (CiA 402): Fault = bit 3; Operation Enabled = (sw & 0x6F) == 0x27.
FAULT_BIT = 0x08
AL_OP = 8


@dataclass
class Trigger:
    seq: int
    time_ns: int
    reason: str
    detail: str = ""


@dataclass
class PendingDump:
    first_trigger: Trigger
    triggers: list[Trigger] = field(default_factory=list)
    until_seq: int = 0


@dataclass
class Settings:
    history_s: float = 30.0
    pre_s: float = 10.0
    post_s: float = 2.0
    max_window_s: float = 60.0
    interval_s: float = 0.05
    process_data_age_ms: float = 3.0
    overrun_factor: float = 1.5  # counted in the cycle statistics
    overrun_trigger_factor: float = 3.0  # saves a window (a whole cycle was lost)
    stall_s: float = 0.2


class TriggerScanner:
    """Edge-triggered checks on the configured fields; the first cycle only initialises state."""

    def __init__(self, fields: list[str], settings: Settings):
        self.settings = settings
        index = {name: i for i, name in enumerate(fields)}
        self.status = [(name.rsplit("/", 1)[0], i) for name, i in index.items() if name.endswith("/status_word")]
        self.al = [(name.rsplit("/", 1)[0], i) for name, i in index.items() if name.endswith("/al_state")]
        self.wkc = index.get("ethercat_master/wc_error_count")
        self.age = index.get("ethercat_domain/process_data_age_ms")
        self.responding = index.get("ethercat_master/slaves_responding")
        self.link = index.get("ethercat_master/link_up")
        self.previous: dict[str, float] = {}
        self.nominal_ns: Optional[float] = None
        self._periods: list[int] = []
        self.stats = {"cycles": 0, "overruns": 0, "max_period_ns": 0, "min_period_ns": None, "sum_period_ns": 0}

    def _edge(self, key: str, value: float) -> Optional[float]:
        before = self.previous.get(key)
        self.previous[key] = value
        return before

    def scan(self, seq: int, slot: bytes) -> list[Trigger]:
        _, time_ns, period_ns = SLOT_HEAD.unpack_from(slot, 0)
        found: list[Trigger] = []

        def value(i: int) -> float:
            return F64.unpack_from(slot, 24 + 8 * i)[0]

        def add(reason: str, detail: str = "") -> None:
            found.append(Trigger(seq, time_ns, reason, detail))

        stats = self.stats
        stats["cycles"] += 1
        if period_ns > 0:
            if self.nominal_ns is None:
                self._periods.append(period_ns)
                if len(self._periods) >= 200:
                    self.nominal_ns = sorted(self._periods)[len(self._periods) // 2]
            elif period_ns > self.nominal_ns * self.settings.overrun_factor:
                stats["overruns"] += 1
                if period_ns > self.nominal_ns * self.settings.overrun_trigger_factor:
                    add("cycle_overrun", f"period {period_ns / 1e6:.3f} ms")
            stats["max_period_ns"] = max(stats["max_period_ns"], period_ns)
            stats["min_period_ns"] = period_ns if stats["min_period_ns"] is None else min(stats["min_period_ns"], period_ns)
            stats["sum_period_ns"] += period_ns

        for joint, i in self.status:
            word = int(value(i))
            before = self._edge(f"sw:{joint}", word)
            if before is None:
                continue
            before = int(before)
            if word & FAULT_BIT and not before & FAULT_BIT:
                add("drive_fault", f"{joint} status 0x{word:04X}")
            if (before & 0x6F) == 0x27 and (word & 0x6F) != 0x27:
                add("left_operation_enabled", f"{joint} 0x{before:04X} -> 0x{word:04X}")
        for slave, i in self.al:
            state = value(i)
            before = self._edge(f"al:{slave}", state)
            if before == AL_OP and state != AL_OP:
                add("slave_left_op", f"{slave} AL {int(state)}")
        if self.wkc is not None:
            count = value(self.wkc)
            before = self._edge("wkc", count)
            if before is not None and count > before:
                add("wkc_error", f"wc_error_count {int(before)} -> {int(count)}")
        if self.age is not None:
            age = value(self.age)
            before = self._edge("age", age)
            limit = self.settings.process_data_age_ms
            if before is not None and age > limit >= before:
                add("process_data_stale", f"age {age:.1f} ms")
        if self.responding is not None:
            count = value(self.responding)
            before = self._edge("responding", count)
            if before is not None and count < before:
                add("slaves_responding_drop", f"{int(before)} -> {int(count)}")
        if self.link is not None:
            link = value(self.link)
            before = self._edge("link", link)
            if before == 1.0 and link != 1.0:
                add("link_down")
        return found

    def summary(self) -> dict:
        stats = dict(self.stats)
        cycles = max(1, stats["cycles"])
        stats["mean_period_ns"] = round(stats.pop("sum_period_ns") / cycles, 1)
        stats["nominal_period_ns"] = self.nominal_ns
        return stats


class BlackBox(threading.Thread):
    def __init__(
        self,
        path: Path,
        out_dir: Path,
        settings: Optional[Settings] = None,
        explain: Optional[Callable[[float, float], Optional[str]]] = None,
        kernel_lines: Optional[Callable[[float, float], list[str]]] = None,
        on_event: Optional[Callable[[dict], None]] = None,
    ):
        super().__init__(name=f"blackbox-{path.name}", daemon=True)
        self.path = path
        self.reader = ShmReader(path)
        self.out_dir = out_dir
        self.settings = settings or Settings()
        self.explain = explain or (lambda start, end: None)
        self.kernel_lines = kernel_lines or (lambda start, end: [])
        self.on_event = on_event or (lambda event: None)
        self.scanner = TriggerScanner(self.reader.fields, self.settings)
        self.history: collections.deque[tuple[int, bytes]] = collections.deque()
        self.external: "queue.Queue[tuple[str, Optional[float], str]]" = queue.Queue()
        self.pending: Optional[PendingDump] = None
        self.stop_requested = threading.Event()
        self.gaps = 0
        self.saved: list[str] = []
        self.expected: list[dict] = []
        self.last_head = 0
        self.last_head_change = time.monotonic()
        self.writer_stopped_reported = False

    # ------------------------------------------------------------------ public (any thread)
    def trigger(self, reason: str, epoch_s: Optional[float] = None, detail: str = "") -> None:
        self.external.put((reason, epoch_s, detail))

    def stop(self) -> None:
        self.stop_requested.set()

    def describe(self) -> dict:
        return {
            "segment": self.path.name,
            "writer": self.reader.writer_name,
            "writer_pid": self.reader.writer_pid,
            "fields": len(self.reader.fields),
            "gaps": self.gaps,
            "cycle_stats": self.scanner.summary(),
            "saved": list(self.saved),
            "expected_transitions": list(self.expected[-50:]),
        }

    # ------------------------------------------------------------------ thread
    @property
    def _rate_hz(self) -> float:
        nominal = self.scanner.nominal_ns or 1e6
        return 1e9 / nominal

    def run(self) -> None:
        try:
            while not self.stop_requested.is_set():
                self._step(final=False)
                self.stop_requested.wait(self.settings.interval_s)
            self._step(final=True)
        except Exception:
            LOG.exception("black box %s failed", self.path.name)

    def _step(self, final: bool) -> None:
        batch = self.reader.read_raw()
        self.gaps += batch.gaps
        now = time.monotonic()
        if batch.head != self.last_head:
            self.last_head, self.last_head_change = batch.head, now
            self.writer_stopped_reported = False
        keep = int(self.settings.history_s * self._rate_hz)
        for seq, slot in batch.slots:
            self.history.append((seq, slot))
            for found in self.scanner.scan(seq, slot):
                self._add_trigger(found)
        while len(self.history) > keep:
            self.history.popleft()
        self._external_triggers()
        writer_alive = self.reader.writer_alive()
        stalled = now - self.last_head_change > self.settings.stall_s
        if self.history and not self.writer_stopped_reported and self.reader.writer_state == "active" and (stalled or not writer_alive):
            seq, slot = self.history[-1]
            reason = "writer_died" if not writer_alive else "writer_stalled"
            self._add_trigger(Trigger(seq, SLOT_HEAD.unpack_from(slot, 0)[1], reason, f"head {self.last_head}"))
            self.writer_stopped_reported = True
        if self.pending is not None:
            writer_done = final or not writer_alive or self.reader.writer_state != "active" or stalled
            if self.history and (self.history[-1][0] >= self.pending.until_seq or writer_done):
                self._dump()

    def _external_triggers(self) -> None:
        while True:
            try:
                reason, epoch_s, detail = self.external.get_nowait()
            except queue.Empty:
                return
            if not self.history:
                continue
            if epoch_s is None:
                seq, slot = self.history[-1]
            else:
                times = [SLOT_HEAD.unpack_from(slot, 0)[1] for _, slot in self.history]
                index = bisect.bisect_right(times, int(epoch_s * 1e9)) - 1
                if index < 0:
                    continue  # before the oldest kept cycle
                seq, slot = self.history[index]
            self._add_trigger(Trigger(seq, SLOT_HEAD.unpack_from(slot, 0)[1], reason, detail))

    def _add_trigger(self, found: Trigger) -> None:
        post = int(self.settings.post_s * self._rate_hz)
        limit = int(self.settings.max_window_s * self._rate_hz)
        if self.pending is None:
            self.pending = PendingDump(found, [found], found.seq + post)
            return
        self.pending.triggers.append(found)
        self.pending.until_seq = min(max(self.pending.until_seq, found.seq + post), self.pending.first_trigger.seq + limit)

    def _dump(self) -> None:
        pending, self.pending = self.pending, None
        rate = self._rate_hz
        first = pending.first_trigger
        start_seq = first.seq - int(self.settings.pre_s * rate)
        window = [(seq, slot) for seq, slot in self.history if start_seq <= seq <= pending.until_seq]
        if not window:
            return
        t_first = first.time_ns / 1e9
        reasons = sorted({t.reason for t in pending.triggers})
        explanation = self.explain(t_first - 1.0, pending.triggers[-1].time_ns / 1e9 + 2.0)
        if explanation and set(reasons) <= {"left_operation_enabled", "slave_left_op", "process_data_stale", "link_down",
                                           "slaves_responding_drop", "writer_stalled", "writer_died", "kernel_error", "wkc_error",
                                           "cycle_overrun"}:
            record = {"time": t_first, "reasons": reasons, "explained_by": explanation}
            self.expected.append(record)
            self.on_event({"kind": "blackbox_expected", "ts": t_first, "reasons": reasons, "explained_by": explanation})
            return
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t_first)) + f".{int((t_first % 1) * 1000):03d}"
        target = self.out_dir / f"{stamp}-{first.reason}"
        target.mkdir(parents=True, exist_ok=True)
        t_start = SLOT_HEAD.unpack_from(window[0][1], 0)[1] / 1e9
        t_end = SLOT_HEAD.unpack_from(window[-1][1], 0)[1] / 1e9
        write_mcap(target / "window.mcap", self.reader, window)
        (target / "kernel.log").write_text("\n".join(self.kernel_lines(t_start, t_end)) + "\n", encoding="utf-8")
        document = {
            "segment": self.path.name,
            "writer": self.reader.writer_name,
            "reasons": reasons,
            "triggers": [t.__dict__ for t in pending.triggers[:200]],
            "trigger_count": len(pending.triggers),
            "window": {"first_seq": window[0][0], "last_seq": window[-1][0], "start": t_start, "end": t_end,
                       "cycles": len(window), "missing": window[-1][0] - window[0][0] + 1 - len(window)},
            "fields": self.reader.fields,
        }
        (target / "trigger.json").write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        self.saved.append(target.name)
        self.on_event({"kind": "blackbox_saved", "ts": t_first, "reasons": reasons, "dir": target.name,
                       "detail": "; ".join(f"{t.reason}: {t.detail}" for t in pending.triggers[:5])})
        LOG.info("black box saved %s (%d cycles)", target, len(window))


def write_mcap(path: Path, reader: ShmReader, window: list[tuple[int, bytes]]) -> None:
    from mcap.writer import CompressionType, Writer

    schema = {"type": "object", "properties": {"seq": {"type": "integer"}, "period_us": {"type": "number"},
                                                **{name: {"type": "number"} for name in reader.fields}}}
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        writer = Writer(stream, compression=CompressionType.ZSTD)
        writer.start(profile="", library="runtime-log")
        schema_id = writer.register_schema("runtime_log/Cycle", "jsonschema", json.dumps(schema).encode())
        channel = writer.register_channel(f"/runtime_log/{reader.writer_name}", "json", schema_id)
        names = reader.fields
        for seq, slot in window:
            time_ns, period_ns, values = reader.decode(slot)
            message = {"seq": seq, "period_us": period_ns / 1000.0}
            message.update(zip(names, values))
            writer.add_message(channel, log_time=time_ns, data=json.dumps(message, separators=(",", ":")).encode(), publish_time=time_ns, sequence=seq & 0xFFFFFFFF)
        writer.finish()
    temporary.replace(path)
