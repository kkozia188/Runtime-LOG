"""Read-only anomaly feed for other pages (the robot status page): saved black-box windows, anomaly
counts and real-time cycle statistics of the running runs, with links into this station.

Only files the recorder already writes are read (run.json, blackbox/*/trigger.json); nothing is computed
from shared memory here, so a request never touches the 1 kHz segments.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .archive import RUN_FILE, Archive, read_json
from .blackbox import Settings
from .timeline import is_anomaly_key_informational

TRIGGERS_PER_WINDOW = 20


def cycle(blackbox: list[dict]) -> Optional[dict]:
    """Cycle statistics since the run started. Every recorder segment of a run is written by the same
    control loop, so counts are not summed across segments: the segment with the most cycles is used."""
    rows = [box for box in blackbox if (box.get("cycle_stats") or {}).get("nominal_period_ns")]
    if not rows:
        return None
    box = max(rows, key=lambda b: b["cycle_stats"].get("cycles") or 0)
    stats = box["cycle_stats"]
    nominal = stats["nominal_period_ns"]
    deviations = [abs(stats[key] - nominal) for key in ("max_period_ns", "min_period_ns") if stats.get(key) is not None]
    return {
        "segment": box.get("segment"),
        "cycles": stats.get("cycles"),
        "nominal_period_us": round(nominal / 1e3, 3),
        "max_jitter_us": round(max(deviations) / 1e3, 1) if deviations else None,
        "overruns": stats.get("overruns"),
        "overrun_rule": f"period > {Settings().overrun_factor:g} x nominal",
    }


def _subjects(triggers: list[dict]) -> list[str]:
    """First word of each trigger detail: the joint, slave or counter the trigger was raised on."""
    names = {str(t.get("detail") or "").split(" ", 1)[0] for t in triggers}
    return sorted(name for name in names if name and not name[0].isdigit())


def windows(run_dir: Path, run_id: str, since: Optional[float]) -> list[dict]:
    rows = []
    for path in sorted((run_dir / "blackbox").glob("*/trigger.json")):
        document = read_json(path, {}) or {}
        triggers = document.get("triggers") or []
        first = min((t.get("time_ns") or 0 for t in triggers), default=0) / 1e9 or None
        if first is None:
            first = (document.get("window") or {}).get("start")
        if since is not None and first is not None and first < since:
            continue
        name = path.parent.name
        rows.append({
            "id": name,
            "time": first,
            "reasons": document.get("reasons") or [],
            "subjects": _subjects(triggers),
            "triggers": [{"reason": t.get("reason"), "detail": t.get("detail"), "time": (t.get("time_ns") or 0) / 1e9}
                         for t in triggers[:TRIGGERS_PER_WINDOW]],
            "trigger_count": document.get("trigger_count", len(triggers)),
            "url": f"/{run_id}/blackbox/{name}/",
        })
    return sorted(rows, key=lambda row: row["time"] or 0, reverse=True)


def _update_age(run_dir: Path) -> Optional[float]:
    """Seconds since the recorder last wrote run.json: the cycle statistics are refreshed every
    flush interval, so the page must not present them as continuous real-time values."""
    try:
        return round(max(0.0, time.time() - (run_dir / RUN_FILE).stat().st_mtime), 1)
    except OSError:
        return None


def snapshot(archive: Archive, since: Optional[float] = None) -> dict:
    """Running runs; if none is running, the most recent run (marked running=false)."""
    runs = archive.runs()
    picked = [run for run in runs if run.get("result") == "running"] or runs[:1]
    out = []
    for run in picked:
        run_id = run["id"]
        run_dir = archive.root / run_id
        summary = run.get("summary") or {}
        counts = summary.get("anomalies") or {}
        running = run.get("result") == "running"
        out.append({
            "id": run_id,
            "running": running,
            "recorded_at": summary.get("updated_at") or run.get("end") or run.get("start"),
            "update_age_s": _update_age(run_dir),
            "result": run.get("result"),
            "scope": run.get("scope"),
            "domain": run.get("domain"),
            "start": run.get("start"),
            "end": run.get("end"),
            "report_url": f"/{run_id}/",
            # Cumulative since this run started, refreshed with run.json (about every 5 s).
            "cycle": cycle(run.get("blackbox") or []),
            "anomalies": {key: value for key, value in counts.items() if not is_anomaly_key_informational(key)},
            "blackbox": windows(run_dir, run_id, since),
        })
    return {"time": time.time(), "since": since, "runs": out}


def parse_since(value: str) -> Optional[float]:
    if not value:
        return None
    number = float(value)
    if number != number or number < 0:
        raise ValueError("since must be a non-negative epoch time in seconds")
    return number


__all__ = ["snapshot", "parse_since", "cycle", "windows"]
