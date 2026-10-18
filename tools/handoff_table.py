#!/usr/bin/env python3
"""Handoff disturbance per archived run: startup timing, kernel datagram warnings and master counters
around the moment the cyclic thread takes over (stage ecat_handoff / ecat_all_op).

Usage: handoff_table.py ~/runtime-log/runs [--window 3]
"""

import argparse
import json
from pathlib import Path


def load_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def first(events, rule):
    return next((e["ts"] for e in events if e.get("rule") == rule), None)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", type=Path)
    parser.add_argument("--window", type=float, default=3.0)
    args = parser.parse_args()
    print("run | t_master_active | t_all_op | activation_s | unmatched | timed_out | al_timeout | responding | lost | tx_err | rate")
    for run_dir in sorted(p for p in args.runs.iterdir() if (p / "run.json").exists()):
        events = load_events(run_dir)
        launch = first(events, "launch_started") or (events[0]["ts"] if events else None)
        active, all_op = first(events, "ecat_master_active"), first(events, "ecat_all_op")
        if not (launch and active and all_op):
            continue
        lo, hi = all_op - 0.5, all_op + args.window
        window = [e for e in events if e.get("source") == "kernel" and lo <= e["ts"] <= hi]
        count = lambda rule: sum(e.get("data", {}).get("count", 1) for e in window if e.get("rule") == rule)
        rows = []
        ecat = run_dir / "ethercat.jsonl"
        if ecat.exists():
            rows = [json.loads(line) for line in ecat.read_text().splitlines() if line.strip()]
        before = [r for r in rows if r.get("counters") and r["ts"] < all_op - 0.2]
        after = [r for r in rows if r.get("counters") and r["ts"] > hi]
        lost = tx = "-"
        if before and after:
            lost = after[0]["counters"]["lost_frames"] - before[-1]["counters"]["lost_frames"]
            tx = after[0]["counters"]["tx_errors"] - before[-1]["counters"]["tx_errors"]
        rate = next((e.get("data", {}).get("hz") for e in events if e.get("rule") == "update_rate"), "?")
        print(f"{run_dir.name} | {active - launch:.2f} | {all_op - launch:.2f} | {all_op - active:.2f} | {count('datagrams_unmatched')} | "
              f"{count('datagrams_timed_out')} | {count('al_state_timeout')} | {count('responding')} | {lost} | {tx} | {rate}")


if __name__ == "__main__":
    main()
