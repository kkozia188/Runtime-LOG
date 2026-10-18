#!/usr/bin/env python3
"""Print the latest cycle of every Runtime-LOG recorder segment (read-only).

Usage: shm_peek.py [--filter SUBSTRING]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime_log.shm import ShmReader, segments  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", default="")
    args = parser.parse_args()
    for path in segments():
        reader = ShmReader(path)
        reader.next = max(1, reader.head())
        batch = reader.read()
        print(f"== {path.name} writer={reader.writer_name} state={reader.writer_state} head={batch.head}")
        if not batch.values:
            continue
        print(f"   period_ns={batch.periods_ns[-1]}")
        for name, value in zip(reader.fields, batch.values[-1]):
            if args.filter in name:
                print(f"   {name:45s} {value:.6g}")
        reader.close()


if __name__ == "__main__":
    main()
