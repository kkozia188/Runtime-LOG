#!/usr/bin/env python3
"""On-CPU intervals of each thread on the RT CPU from a sched_switch trace (startup_trace.sh).

Prints, per thread: first/last time, number of runs, longest single run (no sleep), and the
1 ms-bucketed timeline of busy time so bursts (catch-up loops) and handoffs are visible.
Usage: startup_trace_analyze.py <trace> [--threads ros2_control_no,ecat-handoff] [--around T]
"""

import argparse
import collections
import re

LINE = re.compile(
    r"\s(?P<ts>\d+\.\d+): sched_switch: prev_comm=(?P<pc>.+?) prev_pid=(?P<pp>\d+) prev_prio=(?P<ppr>\d+) prev_state=(?P<ps>\S+) ==> "
    r"next_comm=(?P<nc>.+?) next_pid=(?P<np>\d+) next_prio=(?P<npr>\d+)"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace")
    parser.add_argument("--threads", default="ros2_control_no,ecat-handoff")
    parser.add_argument("--min-run-us", type=float, default=300.0)
    args = parser.parse_args()
    wanted = set(args.threads.split(","))
    running: dict[int, float] = {}
    names: dict[int, str] = {}
    prio: dict[int, int] = {}
    runs = collections.defaultdict(list)  # pid -> [(start, end)]
    for line in open(args.trace, errors="replace"):
        m = LINE.search(line)
        if not m:
            continue
        ts = float(m["ts"])
        pp, np_ = int(m["pp"]), int(m["np"])
        if pp in running:
            runs[pp].append((running.pop(pp), ts))
        if m["nc"] in wanted:
            running[np_] = ts
            names[np_] = m["nc"]
            prio[np_] = int(m["npr"])
    for pid, intervals in sorted(runs.items(), key=lambda kv: kv[1][0][0]):
        longest = max(intervals, key=lambda iv: iv[1] - iv[0])
        gaps = [b[0] - a[1] for a, b in zip(intervals, intervals[1:])]
        short = sum(1 for g in gaps if g < 200e-6)
        print(f"pid {pid} {names[pid]} prio {prio[pid]}: {intervals[0][0]:.4f} .. {intervals[-1][1]:.4f}  runs={len(intervals)} "
              f"longest={1e3 * (longest[1] - longest[0]):.2f} ms at {longest[0]:.4f}  off-gaps<200us={short}")
        long_runs = [(a, b) for a, b in intervals if (b - a) * 1e6 >= args.min_run_us]
        for a, b in long_runs[:15]:
            print(f"    long run {a:.4f} +{1e3 * (b - a):.2f} ms")
        # Per 100 ms: number of switch-ins and busy ms, to see cadence changes and bursts.
        buckets = collections.OrderedDict()
        for a, b in intervals:
            key = round(a - a % 0.1, 1)
            n, busy = buckets.get(key, (0, 0.0))
            buckets[key] = (n + 1, busy + (b - a))
        dense = [(k, n, busy) for k, (n, busy) in buckets.items() if n > 105 or busy > 0.03]
        for k, n, busy in dense[:20]:
            print(f"    100ms@{k:.1f}: switch-ins={n} busy={busy * 1e3:.1f} ms")


if __name__ == "__main__":
    main()
