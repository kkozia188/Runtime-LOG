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

from . import cia402
from .experiment import Experiment, has_tracking_fields
from .shm import ShmReader

LOG = logging.getLogger("runtime_log.blackbox")
F64 = struct.Struct("<d")
SLOT_HEAD = struct.Struct("<Qqq")

# Status word (CiA 402): Fault = bit 3; Operation Enabled = (sw & 0x6F) == 0x27.
FAULT_BIT = 0x08
AL_OP = 8


# Reasons that an orderly disable makes expected; a stop additionally explains power and quick stop.
EXPLAINED_BY_DISABLE = frozenset({
    "left_operation_enabled", "slave_left_op", "process_data_stale", "link_down", "slaves_responding_drop",
    "writer_stalled", "writer_died", "kernel_error", "wkc_error", "cycle_overrun",
})
EXPLAINED_BY_SHUTDOWN = EXPLAINED_BY_DISABLE | {"power_lost", "quick_stop"}


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
    experiment_max_s: float = 600.0
    # {field: {title, names, labels, trigger}} for enum fields of instrumented controllers.
    enums: dict = field(default_factory=dict)


def enum_name(spec: dict, number: float) -> tuple[str, str]:
    index = int(number) if number == number else -1
    names, labels = spec.get("names", []), spec.get("labels", [])
    name = names[index] if 0 <= index < len(names) else f"#{index}"
    return name, labels[index] if 0 <= index < len(labels) else name


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
        self.enums = [(name, index[name], spec) for name, spec in settings.enums.items() if name in index]
        self.state_events: list[dict] = []  # drained by the black box after each scan
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
            if before is None or int(before) == word:
                continue
            before = int(before)
            old_state, new_state = cia402.state(before), cia402.state(word)
            change = f"{joint} {cia402.describe(before)} -> {cia402.describe(word)}"
            if word & FAULT_BIT and not before & FAULT_BIT:
                add("drive_fault", change)
            if old_state == "operation_enabled" and new_state != "operation_enabled":
                add("left_operation_enabled", change)
            if before & cia402.VOLTAGE_ENABLED and not word & cia402.VOLTAGE_ENABLED and old_state in cia402.POWERED_STATES:
                add("power_lost", change)
            if new_state == "quick_stop_active" and old_state != "quick_stop_active":
                add("quick_stop", change)
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
        for name, i, spec in self.enums:
            number = value(i)
            before = self._edge(f"enum:{name}", number)
            if before is None or before == number:
                continue
            new_name, new_label = enum_name(spec, number)
            old_name, old_label = enum_name(spec, before)
            self.state_events.append({"kind": "state_change", "ts": time_ns / 1e9, "field": name, "title": spec.get("title", name),
                                      "old": old_name, "new": new_name, "old_label": old_label, "new_label": new_label})
            if new_name in spec.get("trigger", []):
                add(f"{name}:{new_name}", f"{spec.get('title', name)}：{old_label} -> {new_label}")
        return found

    def drive_states(self) -> dict:
        return {key[3:]: cia402.decode(int(word)) for key, word in self.previous.items() if key.startswith("sw:")}

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
        # M2 experiments (only for segments that carry position + target_position).
        self.experiments_dir = out_dir.parent / "experiments"
        self.tracking = has_tracking_fields(self.reader.fields)
        self.experiment_commands: "queue.Queue[tuple]" = queue.Queue()
        self.experiment: Optional[Experiment] = None
        self.experiments: list[dict] = []

    # ------------------------------------------------------------------ public (any thread)
    def trigger(self, reason: str, epoch_s: Optional[float] = None, detail: str = "") -> None:
        self.external.put((reason, epoch_s, detail))

    def stop(self) -> None:
        self.stop_requested.set()

    def start_experiment(self, name: str, source: str, start_ts: float, meta: Optional[dict] = None) -> bool:
        if not self.tracking:
            return False
        self.experiment_commands.put(("start", name, source, start_ts, meta or {}))
        return True

    def stop_experiment(self, stop_ts: float) -> None:
        if self.tracking:
            self.experiment_commands.put(("stop", stop_ts))

    def describe(self) -> dict:
        return {
            "segment": self.path.name,
            "writer": self.reader.writer_name,
            "writer_pid": self.reader.writer_pid,
            "fields": len(self.reader.fields),
            "gaps": self.gaps,
            "cycle_stats": self.scanner.summary(),
            "drive_states": self.scanner.drive_states(),
            "saved": list(self.saved),
            "expected_transitions": list(self.expected[-50:]),
            "experiments": list(self.experiments),
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
            if self.scanner.state_events:
                for event in self.scanner.state_events:
                    self.on_event(event)
                self.scanner.state_events.clear()
            if self.experiment is not None:
                self._experiment_slot(seq, slot)
        self._experiment_requests()
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
        if self.experiment is not None and (final or not writer_alive or self.reader.writer_state != "active"):
            last_time = SLOT_HEAD.unpack_from(self.history[-1][1], 0)[1] / 1e9 if self.history else self.experiment.start_ts
            self._finish_experiment(min(self.experiment.stop_ts or last_time, last_time))
        if self.pending is not None:
            writer_done = final or not writer_alive or self.reader.writer_state != "active" or stalled
            if self.history and (self.history[-1][0] >= self.pending.until_seq or writer_done):
                self._dump()

    # ------------------------------------------------------------------ experiments
    def _experiment_slot(self, seq: int, slot: bytes) -> None:
        experiment = self.experiment
        t = SLOT_HEAD.unpack_from(slot, 0)[1] / 1e9
        if t < experiment.start_ts:
            return
        if experiment.stop_ts is not None and t > experiment.stop_ts:
            self._finish_experiment(experiment.stop_ts)
            return
        experiment.add(seq, slot)
        if t - experiment.start_ts >= experiment.max_duration_s:
            experiment.meta["truncated"] = f"max {experiment.max_duration_s:.0f} s"
            self._finish_experiment(t)

    def _experiment_requests(self) -> None:
        backfill = False
        while True:
            try:
                command = self.experiment_commands.get_nowait()
            except queue.Empty:
                break
            if command[0] == "stop":
                if self.experiment is not None:
                    self.experiment.stop_ts = command[1]
                continue
            _, name, source, start_ts, meta = command
            if self.experiment is not None:
                if backfill:
                    self._backfill()
                self._finish_experiment(start_ts)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start_ts)) + f".{int((start_ts % 1) * 1000):03d}"
            safe = "".join(c if c.isascii() and (c.isalnum() or c in "_-") else "_" for c in name)[:60]
            self.experiment = Experiment(self.experiments_dir / f"{stamp}-{safe}", name, source, self.reader.fields,
                                         self.reader.writer_name, start_ts, meta, self.settings.experiment_max_s)
            backfill = True
        if self.experiment is None:
            return
        if backfill:
            self._backfill()
        stop = self.experiment.stop_ts
        last = SLOT_HEAD.unpack_from(self.history[-1][1], 0)[1] / 1e9 if self.history else 0.0
        if stop is not None and last >= stop:
            self._finish_experiment(stop)

    def _backfill(self) -> None:
        """Cycles of the new experiment already in the 30 s history, within [start, stop]."""
        experiment = self.experiment
        for seq, slot in self.history:
            t = SLOT_HEAD.unpack_from(slot, 0)[1] / 1e9
            if t >= experiment.start_ts and (experiment.stop_ts is None or t <= experiment.stop_ts):
                experiment.add(seq, slot)

    def _finish_experiment(self, stop_ts: float) -> None:
        experiment, self.experiment = self.experiment, None
        if experiment is None:
            return
        experiment.stop_ts = stop_ts
        try:
            document = experiment.finish()
        except Exception:  # noqa: BLE001 - never let a metric bug stop the black box
            LOG.exception("experiment %s failed", experiment.name)
            return
        entry = {"dir": experiment.directory.name, "name": experiment.name, "source": experiment.source,
                 "start": experiment.start_ts, "duration_s": document["duration_s"], "summary": document["summary"]}
        self.experiments.append(entry)
        self.on_event({"kind": "experiment_saved", "ts": experiment.start_ts, **entry})

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

    # A running experiment ends 1 s after the drives stop following (no JTC log line comes then).
    EXPERIMENT_STOP_REASONS = ("drive_fault", "left_operation_enabled", "power_lost", "quick_stop", "writer_died", "writer_stalled")

    def _add_trigger(self, found: Trigger) -> None:
        if self.experiment is not None and found.reason in self.EXPERIMENT_STOP_REASONS:
            stop = found.time_ns / 1e9 + 1.0
            if self.experiment.stop_ts is None or stop < self.experiment.stop_ts:
                self.experiment.stop_ts = stop
                self.experiment.meta["stopped_by"] = f"{found.reason}: {found.detail}"[:200]
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
        explainable = EXPLAINED_BY_SHUTDOWN if explanation in ("shutdown", "run ended") else EXPLAINED_BY_DISABLE
        if explanation and set(reasons) <= explainable:
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
