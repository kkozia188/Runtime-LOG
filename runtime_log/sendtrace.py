"""EtherCAT send trace written by the patched ICube driver (RT_CONTROL_ECAT_TRACE_FILE).

Files (written on orderly deactivation): <path> raw samples (first window), <path>.stats.csv per
phase (startup / maintenance / control: samples, gaps, min/max gap, incomplete WKC), and
<path>.hist.csv (1 µs gap histogram). A send burst (gaps far below the cycle) is the signature of
catch-up sending, e.g. at the startup -> cyclic handoff.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

BURST_GAP_US = 500


def find_send_trace(pattern: str, start: float, end: float, slack_s: float = 120.0) -> Optional[Path]:
    """Newest file matching `pattern` (~ expanded, glob) written during [start, end + slack].

    Used when the control process hides its environment (a launch with capabilities the reader
    lacks), so RT_CONTROL_ECAT_TRACE_FILE cannot be read: the driver writes the trace on stop.
    """
    import glob
    import os

    candidates = []
    for name in glob.glob(os.path.expanduser(pattern)):
        try:
            mtime = os.stat(name).st_mtime
        except OSError:
            continue
        if start <= mtime <= end + slack_s:
            candidates.append((mtime, name))
    return Path(max(candidates)[1]) if candidates else None


def read_send_trace(path: Path, burst_gap_us: int = BURST_GAP_US) -> Optional[dict]:
    stats_path = Path(f"{path}.stats.csv")
    if not stats_path.exists():
        return None
    result: dict = {"path": str(path), "phases": {}}
    with stats_path.open() as stream:
        for row in csv.DictReader(stream):
            result["phases"][row["phase"]] = {key: int(value) for key, value in row.items() if key != "phase"}
    histogram: dict = {}
    hist_path = Path(f"{path}.hist.csv")
    if hist_path.exists():
        with hist_path.open() as stream:
            for row in csv.DictReader(stream):
                histogram.setdefault(row["phase"], []).append((int(row["gap_us"]), int(row["count"])))
    for phase, values in histogram.items():
        entry = result["phases"].setdefault(phase, {})
        entry["gaps_below_burst"] = sum(count for gap, count in values if gap < burst_gap_us)
        entry["gaps_above_1_5x"] = sum(count for gap, count in values if gap > 1500)
    # Raw window around the handoff: first control samples and the last startup/maintenance ones.
    raw = Path(path)
    if raw.exists():
        with raw.open() as stream:
            rows = list(csv.DictReader(stream))
        switch = next((i for i, r in enumerate(rows) if r.get("phase") == "control"), None)
        if switch is not None:
            window = rows[max(0, switch - 5): switch + 30]
            t0 = int(rows[switch]["monotonic_ns"])
            result["handoff_window"] = [
                {"t_us": round((int(r["monotonic_ns"]) - t0) / 1000, 1), "phase": r["phase"], "wkc": int(r["working_counter"])}
                for r in window
            ]
    result["burst"] = any(entry.get("gaps_below_burst", 0) > 0 for entry in result["phases"].values())
    return result
