"""Run summary from events: result, failure, last good stage, phases, processes, anomaly counts."""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from . import kernlog
from .model import CONTROL, ECAT, ERROR, FAILURE, PROCESS_EXITED, PROCESS_STARTED, SHUTDOWN, SIGNAL, STAGE, WARNING, Event


def sort_events(events: Iterable[Event]) -> list[Event]:
    return sorted(events, key=lambda e: (e.ts, e.source != "launch"))


def summarize(events: list[Event], ready_stages: Iterable[str] = (), running: bool = False, end_ts: Optional[float] = None) -> dict:
    events = sort_events(events)
    launch_events = [e for e in events if e.source == "launch"]
    t0 = launch_events[0].ts if launch_events else (events[0].ts if events else 0.0)
    end = end_ts if end_ts is not None else (events[-1].ts if events else t0)

    shutdown_ts = next((e.ts for e in events if e.kind in (SHUTDOWN, SIGNAL)), None)

    failure: Optional[Event] = next((e for e in events if e.kind == FAILURE), None)
    if failure is None:
        # A non-zero exit before any shutdown request is a failure; after it, exits are expected.
        failure = next(
            (
                e
                for e in events
                if e.kind == PROCESS_EXITED
                and e.data.get("exit_code", 0) != 0
                and (shutdown_ts is None or e.ts < shutdown_ts)
            ),
            None,
        )
    failure_ts = failure.ts if failure else None

    stage_cut = min(t for t in (failure_ts, shutdown_ts) if t is not None) if (failure_ts or shutdown_ts) else end
    stages = [e for e in events if e.kind == STAGE and e.ts <= stage_cut]
    last_stage = stages[-1] if stages else None
    ready = [e for e in stages if e.rule in set(ready_stages)]
    ready_ts = ready[-1].ts if ready else None

    if failure is not None:
        result = "failed"
        if failure.kind == FAILURE:
            failed_stage = failure.data.get("stage", failure.rule)
        else:
            failed_stage = f"process_exit:{failure.data.get('base', failure.subject)}"
    elif running:
        result, failed_stage = "running", ""
    elif shutdown_ts is not None:
        result, failed_stage = "ok", ""
    else:
        result, failed_stage = "aborted", ""

    processes = _processes(events, end)
    anomalies = _anomalies(events, shutdown_ts)
    phases = {
        "startup": [0.0, round((ready_ts or stage_cut) - t0, 3)],
        "running": [round(ready_ts - t0, 3), round((shutdown_ts or end) - t0, 3)] if ready_ts else None,
        "stopping": [round(shutdown_ts - t0, 3), round(end - t0, 3)] if shutdown_ts else None,
    }
    return {
        "t0": t0,
        "end": end,
        "duration_s": round(end - t0, 3),
        "result": result,
        "failed_stage": failed_stage,
        "failure": failure.to_dict(t0) if failure else None,
        "last_stage": last_stage.to_dict(t0) if last_stage else None,
        "ready_s": round(ready_ts - t0, 3) if ready_ts else None,
        "shutdown_s": round(shutdown_ts - t0, 3) if shutdown_ts else None,
        "phases": phases,
        "processes": processes,
        "controllers": _controllers(events, t0),
        "anomalies": anomalies,
        "anomaly_total": sum(anomalies.values()),
    }


def phase_of(t_rel: float, summary: dict) -> str:
    phases = summary.get("phases") or {}
    for name in ("stopping", "running", "startup"):
        window = phases.get(name)
        if window and window[0] <= t_rel <= window[1] + 1e-9:
            return name
    return "startup" if t_rel < (summary.get("ready_s") or 0) else "running"


def _processes(events: list[Event], end: float) -> list[dict]:
    table: dict[str, dict] = {}
    for event in events:
        if event.kind == PROCESS_STARTED:
            table[event.subject] = {
                "name": event.subject,
                "base": event.data.get("base", event.subject),
                "pid": event.data.get("pid"),
                "start": event.ts,
                "end": None,
                "exit_code": None,
            }
        elif event.kind == PROCESS_EXITED and event.subject in table:
            table[event.subject]["end"] = event.ts
            table[event.subject]["exit_code"] = event.data.get("exit_code")
    for row in table.values():
        if row["end"] is None:
            row["end_open"] = True
            row["end"] = end
    return sorted(table.values(), key=lambda r: r["start"])


def _controllers(events: list[Event], t0: float) -> list[dict]:
    rows = []
    for event in events:
        if event.kind == STAGE and event.data.get("controller"):
            rows.append({"name": event.data["controller"], "step": event.rule, "t_rel_s": round(event.ts - t0, 3)})
    return rows


def _anomalies(events: list[Event], shutdown_ts: Optional[float]) -> dict:
    counts: Counter = Counter()
    for event in events:
        if event.kind == ECAT and event.rule == "counter_increase":
            for key in ("lost_frames", "tx_errors"):
                if event.data.get(key):
                    counts[f"ecat.{key}"] += int(event.data[key])
        elif event.kind == ECAT and event.rule in kernlog.ANOMALY_RULES:
            amount = event.data.get("count", 1) if event.rule.startswith("datagrams_") else 1
            counts[f"ecat.{event.rule}"] += int(amount)
        elif event.kind == ERROR:
            counts["log.error_lines"] += 1
        elif event.kind == FAILURE:
            counts["log.failures"] += 1
        elif event.kind == PROCESS_EXITED and event.data.get("exit_code", 0) != 0:
            key = "process.died_before_shutdown" if shutdown_ts is None or event.ts < shutdown_ts else "process.nonzero_exit_during_shutdown"
            counts[key] += 1
        elif event.kind == WARNING:
            counts["log.warning_lines"] += 1
    return dict(sorted(counts.items()))


def is_anomaly_key_informational(key: str) -> bool:
    """Counts shown but not highlighted (expected noise)."""
    return key in ("log.warning_lines", "process.nonzero_exit_during_shutdown")


__all__ = ["summarize", "sort_events", "phase_of", "CONTROL"]
