#!/usr/bin/env python3
"""Benchmark reader: batch-read a ring every --interval seconds; report cycles, gaps and batch cost.

Usage: shm_read_bench.py /dev/shm/runtime-log.bench --interval 0.05 --duration 120
"""

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_log.shm import ShmReader  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--duration", type=float, default=60.0)
    args = parser.parse_args()
    deadline = time.time() + 30
    while not args.path.exists() and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    reader = ShmReader(args.path)
    cycles = gaps = 0
    costs = []
    end = time.time() + args.duration
    while time.time() < end:
        start = time.perf_counter()
        batch = reader.read()
        costs.append(time.perf_counter() - start)
        cycles += len(batch.seqs)
        gaps += batch.gaps
        if reader.writer_state == "inactive":
            break
        time.sleep(args.interval)
    costs.sort()
    print(f"reader cpu={sorted(os.sched_getaffinity(0))[:3]}... cycles={cycles} gaps={gaps} batches={len(costs)} "
          f"batch_ms p50={costs[len(costs) // 2] * 1e3:.2f} max={costs[-1] * 1e3:.2f}")


if __name__ == "__main__":
    main()
