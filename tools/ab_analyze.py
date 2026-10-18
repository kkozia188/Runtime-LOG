#!/usr/bin/env python3
"""Analyse tools/ab_jitter.sh output: RT-thread switch-in period statistics and CPU contamination.

Usage: ab_analyze.py <out_dir> [--period-us 1000]
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter
from pathlib import Path

LINE = re.compile(
    r"\[(?P<cpu>\d+)\].*?\s(?P<ts>\d+\.\d+): sched_switch: prev_comm=(?P<prev_comm>.+?) prev_pid=(?P<prev_pid>\d+) .*? ==> "
    r"next_comm=(?P<next_comm>.+?) next_pid=(?P<next_pid>\d+) next_prio=(?P<next_prio>\d+)"
)
# Expected visitors of an isolated RT CPU: idle, per-CPU kernel threads.
KERNEL_PER_CPU = re.compile(r"^(swapper/\d+|migration/\d+|ksoftirqd/\d+|kworker/\d+:.*|cpuhp/\d+|rcuc/\d+|ktimers/\d+|irq_work/\d+|idle_inject/\d+|EtherCAT-OP|EtherCAT-IDLE)$")


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def analyse(path: Path, rt_tid: int, period_us: float) -> dict:
    switch_ins: list[float] = []
    visitors: Counter = Counter()
    for line in path.read_text(errors="replace").splitlines():
        match = LINE.search(line)
        if not match:
            continue
        pid = int(match["next_pid"])
        if pid == rt_tid:
            switch_ins.append(float(match["ts"]))
        else:
            visitors[f'{match["next_comm"]}:{pid}' if not KERNEL_PER_CPU.match(match["next_comm"]) else match["next_comm"]] += 1
    gaps = [(b - a) * 1e6 for a, b in zip(switch_ins, switch_ins[1:])]
    # Several switch-ins inside one cycle (the thread blocked mid-cycle) show up as short gaps; keep
    # the cycle-to-cycle view by merging gaps shorter than half a period into the next one.
    cycles, pending = [], 0.0
    for gap in gaps:
        pending += gap
        if pending >= period_us / 2:
            cycles.append(pending)
            pending = 0.0
    if not cycles:
        return {"events": len(switch_ins)}
    deviation = [abs(c - period_us) for c in cycles]
    foreign = {k: v for k, v in visitors.items() if not KERNEL_PER_CPU.match(k.split(":")[0])}
    return {
        "cycles": len(cycles),
        "duration_s": round(switch_ins[-1] - switch_ins[0], 1),
        "mean_us": round(statistics.fmean(cycles), 3),
        "std_us": round(statistics.pstdev(cycles), 3),
        "min_us": round(min(cycles), 1),
        "max_us": round(max(cycles), 1),
        "dev_p99_us": round(percentile(deviation, 0.99), 1),
        "dev_p999_us": round(percentile(deviation, 0.999), 1),
        "dev_max_us": round(max(deviation), 1),
        "over_50us": sum(d > 50 for d in deviation),
        "over_100us": sum(d > 100 for d in deviation),
        "short_gaps": sum(g < period_us / 2 for g in gaps),
        "kernel_visitors": {k: v for k, v in visitors.items() if KERNEL_PER_CPU.match(k.split(":")[0])},
        "foreign_visitors": foreign,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--period-us", type=float, default=1000.0)
    args = parser.parse_args()
    meta = (args.out / "meta.txt").read_text()
    rt_tid = int(re.search(r"rt_tid=(\d+)", meta)[1])
    print(meta.strip())
    counters = (args.out / "counters.txt").read_text().splitlines() if (args.out / "counters.txt").exists() else []
    for trace in sorted(args.out.glob("*.trace"), key=lambda p: ["off", "log0", "log1", "log5"].index(p.stem) if p.stem in ("off", "log0", "log1", "log5") else 9):
        result = analyse(trace, rt_tid, args.period_us)
        print(f"\n== {trace.stem}")
        for key, value in result.items():
            print(f"  {key}: {value}")
        lines = [c for c in counters if c.startswith(trace.stem + " ")]
        if len(lines) == 2:
            numbers = [dict(re.findall(r"(Tx frames|Rx frames|Lost frames|Tx errors) (\d+)", line)) for line in lines]
            print("  master delta: " + ", ".join(f"{k}=+{int(numbers[1][k]) - int(numbers[0][k])}" for k in numbers[0] if k in numbers[1]))


if __name__ == "__main__":
    main()
