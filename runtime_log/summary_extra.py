"""Summary additions shared by the live recorder and `replay`: 1 kHz cycle statistics, black-box
reader gaps, post-stop drive codes, send-trace bursts and EtherCAT counters per run phase."""

from __future__ import annotations

from .model import Event
from .poststop import summarize as summarize_post_stop
from .timeline import phase_of


def enrich(summary: dict, document: dict, events: list[Event], expected_codes=()) -> dict:
    anomalies = summary["anomalies"]
    boxes = document.get("blackbox") or []
    overruns = sum((box.get("cycle_stats") or {}).get("overruns") or 0 for box in boxes)
    gaps = sum(box.get("gaps") or 0 for box in boxes)
    if overruns:
        anomalies["cycle.overruns"] = overruns
    if gaps:
        anomalies["blackbox.reader_gaps"] = gaps
    anomalies.update(summarize_post_stop(document.get("post_stop"), expected_codes))
    trace = document.get("send_trace") or {}
    burst = sum(phase.get("gaps_below_burst", 0) for phase in (trace.get("phases") or {}).values())
    if burst:
        anomalies["ecat.send_burst"] = burst
    by_phase: dict = {}
    t0 = summary.get("t0") or 0.0
    for event in events:
        if event.rule != "counter_increase":
            continue
        phase = phase_of(event.ts - t0, summary)
        entry = by_phase.setdefault(phase, {"lost_frames": 0, "tx_errors": 0})
        for key in entry:
            entry[key] += int(event.data.get(key, 0) or 0)
    summary["ecat_counters_by_phase"] = by_phase
    summary["anomalies"] = dict(sorted(anomalies.items()))
    summary["anomaly_total"] = sum(summary["anomalies"].values())
    return summary
