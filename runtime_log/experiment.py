"""M2 control performance recording: stream 1 kHz cycles of an experiment to MCAP and compute
per-joint tracking metrics from the commanded target (RxPDO mirror) and the actual position.

An experiment is started/stopped by the black-box thread (auto: one per trajectory goal; manual:
`runtime-log experiment start|stop`). Everything here runs on non-isolated CPUs.
"""

from __future__ import annotations

import json
import math
import struct
import time
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SLOT_HEAD = struct.Struct("<Qqq")
RAD2DEG = 180.0 / math.pi


@dataclass
class MetricSettings:
    settle_tolerance_deg: float = 0.05
    settle_hold_s: float = 0.1
    jitter_window_s: float = 0.5
    # ~2 encoder counts on the bench (2^19 counts/rev): smaller steps are feedback noise of a held target.
    moving_epsilon_rad: float = 2e-5
    max_plot_points: int = 1500


@dataclass
class JointSeries:
    times: array = field(default_factory=lambda: array("d"))
    position: array = field(default_factory=lambda: array("d"))
    target: array = field(default_factory=lambda: array("d"))
    velocity: array = field(default_factory=lambda: array("d"))
    torque: array = field(default_factory=lambda: array("d"))
    status: array = field(default_factory=lambda: array("d"))


def joint_fields(fields: list[str]) -> dict[str, dict[str, int]]:
    """Joints that have both position and target_position, with the indices of their channels."""
    joints: dict[str, dict[str, int]] = {}
    for index, name in enumerate(fields):
        joint, _, interface = name.rpartition("/")
        if interface in ("position", "target_position", "velocity", "torque_permille", "status_word"):
            joints.setdefault(joint, {})[interface] = index
    return {joint: idx for joint, idx in joints.items() if "position" in idx and "target_position" in idx}


def has_tracking_fields(fields: list[str]) -> bool:
    return bool(joint_fields(fields))


class Experiment:
    def __init__(self, directory: Path, name: str, source: str, fields: list[str], writer_name: str,
                 start_ts: float, meta: Optional[dict] = None, max_duration_s: float = 600.0):
        self.directory = directory
        self.name = name
        self.source = source
        self.fields = fields
        self.start_ts = start_ts
        self.stop_ts: Optional[float] = None
        self.max_duration_s = max_duration_s
        self.meta = dict(meta or {})
        self.joints = joint_fields(fields)
        self.series = {joint: JointSeries() for joint in self.joints}
        self.values = struct.Struct(f"<{len(fields)}d")
        self.periods = array("d")
        self.cycles = 0
        self.first_seq: Optional[int] = None
        self.last_seq: Optional[int] = None
        self.missing = 0
        directory.mkdir(parents=True, exist_ok=True)
        from mcap.writer import CompressionType, Writer

        self._stream = (directory / "data.mcap.tmp").open("wb")
        self._writer = Writer(self._stream, compression=CompressionType.ZSTD)
        self._writer.start(profile="", library="runtime-log")
        schema = {"type": "object", "properties": {"seq": {"type": "integer"}, "period_us": {"type": "number"},
                                                    **{name: {"type": "number"} for name in fields}}}
        schema_id = self._writer.register_schema("runtime_log/Cycle", "jsonschema", json.dumps(schema).encode())
        self._channel = self._writer.register_channel(f"/runtime_log/{writer_name}", "json", schema_id)

    def add(self, seq: int, slot: bytes) -> None:
        _, time_ns, period_ns = SLOT_HEAD.unpack_from(slot, 0)
        values = self.values.unpack_from(slot, 24)
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.missing += seq - self.last_seq - 1
        self.first_seq = seq if self.first_seq is None else self.first_seq
        self.last_seq = seq
        self.cycles += 1
        message = {"seq": seq, "period_us": period_ns / 1000.0}
        message.update(zip(self.fields, values))
        self._writer.add_message(self._channel, log_time=time_ns, data=json.dumps(message, separators=(",", ":")).encode(),
                                 publish_time=time_ns, sequence=seq & 0xFFFFFFFF)
        t = time_ns / 1e9
        self.periods.append(period_ns / 1e3)
        for joint, idx in self.joints.items():
            series = self.series[joint]
            series.times.append(t)
            series.position.append(values[idx["position"]])
            series.target.append(values[idx["target_position"]])
            series.velocity.append(values[idx["velocity"]] if "velocity" in idx else math.nan)
            series.torque.append(values[idx["torque_permille"]] if "torque_permille" in idx else math.nan)
            series.status.append(values[idx["status_word"]] if "status_word" in idx else math.nan)

    def finish(self, settings: Optional[MetricSettings] = None) -> dict:
        settings = settings or MetricSettings()
        self._writer.finish()
        self._stream.close()
        (self.directory / "data.mcap.tmp").replace(self.directory / "data.mcap")
        metrics = {joint: joint_metrics(series, settings) for joint, series in self.series.items()}
        moving = {joint: m for joint, m in metrics.items() if m.get("moved")}
        periods = sorted(self.periods)
        document = {
            "name": self.name,
            "source": self.source,
            "start": self.start_ts,
            "end": self.stop_ts,
            "duration_s": round((self.stop_ts or self.start_ts) - self.start_ts, 3),
            "cycles": self.cycles,
            "first_seq": self.first_seq,
            "last_seq": self.last_seq,
            "missing_cycles": self.missing,
            "period_us": {"max": round(periods[-1], 1), "p99": round(periods[int(0.99 * (len(periods) - 1))], 1)} if periods else {},
            "settings": settings.__dict__,
            "joints": metrics,
            "summary": summarize(moving),
            "meta": self.meta,
            "written": time.time(),
        }
        (self.directory / "meta.json").write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.directory / "plot.json").write_text(json.dumps(plot_data(self.series, settings.max_plot_points)), encoding="utf-8")
        return document


def _rms(values) -> float:
    values = [v for v in values if v == v]
    return math.sqrt(sum(v * v for v in values) / len(values)) if values else math.nan


def _enabled(word: float) -> bool:
    return word == word and (int(word) & 0x6F) == 0x27


def truncate_at_disable(series: JointSeries) -> tuple[JointSeries, Optional[float]]:
    """Metrics only cover cycles while the drive follows: cut at the first cycle it leaves
    Operation Enabled (fault, quick stop, disable) if it was enabled at the start."""
    if not series.status or not _enabled(series.status[0]):
        return series, None
    for i, word in enumerate(series.status):
        if not _enabled(word):
            cut = JointSeries()
            for name in ("times", "position", "target", "velocity", "torque", "status"):
                getattr(cut, name).extend(getattr(series, name)[:i])
            return cut, series.times[i]
    return series, None


def joint_metrics(series: JointSeries, settings: MetricSettings) -> dict:
    series, left_at = truncate_at_disable(series)
    n = len(series.times)
    if n == 0:
        return {"samples": 0}
    error = [t - p for t, p in zip(series.target, series.position)]
    target = series.target
    moving_index = [i for i in range(1, n) if abs(target[i] - target[i - 1]) > settings.moving_epsilon_rad]
    out: dict = {"samples": n, "moved": bool(moving_index)}
    if left_at is not None:
        out["left_operation_enabled_at_s"] = round(left_at - series.times[0], 4)
    finite = [abs(e) for e in error if e == e]
    if finite:
        worst = max(range(n), key=lambda i: abs(error[i]) if error[i] == error[i] else -1)
        out.update(max_error_deg=round(abs(error[worst]) * RAD2DEG, 5), max_error_at_s=round(series.times[worst] - series.times[0], 4),
                   rms_error_deg=round(_rms(error) * RAD2DEG, 5))
    velocities = [abs(v) for v in series.velocity if v == v]
    torques = [abs(v) for v in series.torque if v == v]
    if velocities:
        out["peak_velocity_deg_s"] = round(max(velocities) * RAD2DEG, 3)
    if torques:
        out["peak_torque_permille"] = round(max(torques), 1)
    if not moving_index:
        out["static_jitter_deg"] = round(_std(series.position[-_window(series, settings.jitter_window_s):]) * RAD2DEG, 6)
        return out
    first_move, last_move = moving_index[0] - 1, moving_index[-1]
    final = target[-1]
    start_target = target[first_move]
    direction = 1.0 if final >= start_target else -1.0
    out["travel_deg"] = round(abs(final - start_target) * RAD2DEG, 4)
    out["motion_time_s"] = round(series.times[last_move] - series.times[first_move], 4)
    moving_errors = error[first_move:last_move + 1]
    out["rms_error_moving_deg"] = round(_rms(moving_errors) * RAD2DEG, 5)
    overshoot = max((direction * (p - final) for p in series.position[first_move:]), default=0.0)
    out["overshoot_deg"] = round(max(overshoot, 0.0) * RAD2DEG, 5)
    tolerance = settings.settle_tolerance_deg / RAD2DEG
    hold = settings.settle_hold_s
    settle_index = None
    for i in range(last_move, n):
        if abs(final - series.position[i]) > tolerance:
            settle_index = None
        elif settle_index is None:
            settle_index = i
    if settle_index is not None and series.times[-1] - series.times[settle_index] >= hold:
        out["settle_time_s"] = round(series.times[settle_index] - series.times[last_move], 4)
        tail = series.position[max(settle_index, n - _window(series, settings.jitter_window_s)):]
        out["static_jitter_deg"] = round(_std(tail) * RAD2DEG, 6)
        out["final_error_deg"] = round((final - series.position[-1]) * RAD2DEG, 5)
    else:
        out["settle_time_s"] = None
        out["final_error_deg"] = round((final - series.position[-1]) * RAD2DEG, 5)
    return out


def _window(series: JointSeries, seconds: float) -> int:
    if len(series.times) < 2:
        return len(series.times)
    dt = (series.times[-1] - series.times[0]) / (len(series.times) - 1) or 1e-3
    return max(1, int(seconds / dt))


def _std(values) -> float:
    values = [v for v in values if v == v]
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def summarize(moving: dict) -> dict:
    if not moving:
        return {"moving_joints": 0}

    def worst(key):
        values = [(m[key], joint) for joint, m in moving.items() if m.get(key) is not None]
        return max(values) if values else (None, None)

    max_error, max_joint = worst("max_error_deg")
    settle, settle_joint = worst("settle_time_s")
    overshoot, overshoot_joint = worst("overshoot_deg")
    return {
        "moving_joints": len(moving),
        "max_error_deg": max_error, "max_error_joint": max_joint,
        "worst_settle_s": settle, "worst_settle_joint": settle_joint,
        "max_overshoot_deg": overshoot, "max_overshoot_joint": overshoot_joint,
        "unsettled": sorted(joint for joint, m in moving.items() if m.get("settle_time_s") is None),
    }


def plot_data(series: dict[str, JointSeries], max_points: int) -> dict:
    out = {}
    for joint, s in series.items():
        n = len(s.times)
        if n == 0:
            continue
        step = max(1, n // max_points)
        t0 = s.times[0]
        out[joint] = {
            "t": [round(s.times[i] - t0, 4) for i in range(0, n, step)],
            "target_deg": [round(s.target[i] * RAD2DEG, 5) for i in range(0, n, step)],
            "position_deg": [round(s.position[i] * RAD2DEG, 5) for i in range(0, n, step)],
            # Peak-preserving error per bucket so short spikes stay visible after downsampling.
            "error_deg": [round(max((s.target[j] - s.position[j] for j in range(i, min(n, i + step))), key=abs) * RAD2DEG, 5)
                          for i in range(0, n, step)],
        }
    return out
