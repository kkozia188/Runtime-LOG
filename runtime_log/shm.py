"""Reader side of the shared-memory transport ring (format: ros/runtime_log_recorder/.../shm_ring.hpp).

The reader only reads the segment. It never writes progress back, so the real-time writer never
sees it. Correct on x86-64 (TSO): loads are not reordered with older loads, so the
"seq, copy, seq" sequence below needs no explicit fences.
"""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MAGIC = b"RTLOGSHM"
VERSION = 1
BUSY = 2**64 - 1
SHM_DIR = Path("/dev/shm")
PREFIX = "runtime-log."
HEADER = struct.Struct("<8sIIIIIIqqq")  # 56 bytes, line 0
SLOT_HEAD = struct.Struct("<Qqq")
U64 = struct.Struct("<Q")
STATES = {0: "created", 1: "active", 2: "inactive"}


@dataclass
class Batch:
    seqs: list[int]
    times_ns: list[int]
    periods_ns: list[int]
    values: list[tuple]
    gaps: int  # cycles that could not be read (overwritten, torn, or never written)
    head: int


class ShmReader:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        with self.path.open("rb") as stream:
            self.map = mmap.mmap(stream.fileno(), 0, prot=mmap.PROT_READ)
        magic, version, header_size, slot_count, slot_size, field_count, _state, pid, created_rt, created_mono = HEADER.unpack_from(self.map, 0)
        if magic != MAGIC or version != VERSION:
            self.map.close()
            raise ValueError(f"{path}: not a Runtime-LOG v{VERSION} segment")
        self.header_size, self.slot_count, self.slot_size, self.field_count = header_size, slot_count, slot_size, field_count
        self.writer_pid = pid
        self.created_realtime_ns, self.created_monotonic_ns = created_rt, created_mono
        self.writer_name = self._cstring(128, 64)
        self.info = self._cstring(192, 128)
        self.fields = []
        offsets = []
        for index in range(field_count):
            base = 320 + 64 * index
            self.fields.append(self._cstring(base, 56))
            offset, kind = struct.unpack_from("<IB", self.map, base + 56)
            if kind != 1:
                raise ValueError(f"{path}: field {index} has unsupported type {kind}")
            offsets.append(offset)
        if offsets != [24 + 8 * i for i in range(field_count)]:
            raise ValueError(f"{path}: unexpected field layout")
        self.values = struct.Struct(f"<{field_count}d")
        self.next: Optional[int] = None

    def _cstring(self, offset: int, size: int) -> str:
        raw = self.map[offset : offset + size]
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace")

    @property
    def writer_state(self) -> str:
        return STATES.get(struct.unpack_from("<I", self.map, 28)[0], "?")

    def head(self) -> int:
        return U64.unpack_from(self.map, 64)[0]

    def writer_alive(self) -> bool:
        try:
            os.kill(self.writer_pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def read(self, max_cycles: Optional[int] = None) -> Batch:
        """All cycles committed since the previous call (the first call starts at the oldest safe slot)."""
        head = self.head()
        oldest = max(1, head - self.slot_count + 2)  # the slot after head may already be busy
        start = oldest if self.next is None else self.next
        gaps = 0
        if start < oldest:  # fell behind by more than the ring: those cycles are gone
            gaps += oldest - start
            start = oldest
        stop = head if max_cycles is None else min(head, start + max_cycles - 1)
        seqs, times, periods, values = [], [], [], []
        for seq in range(start, stop + 1):
            offset = self.header_size + (seq % self.slot_count) * self.slot_size
            first = U64.unpack_from(self.map, offset)[0]
            if first != seq:
                gaps += 1
                continue
            raw = self.map[offset : offset + self.slot_size]
            if U64.unpack_from(self.map, offset)[0] != seq:
                gaps += 1  # rewritten while copying
                continue
            _, time_ns, period_ns = SLOT_HEAD.unpack_from(raw, 0)
            seqs.append(seq)
            times.append(time_ns)
            periods.append(period_ns)
            values.append(self.values.unpack_from(raw, 24))
        self.next = stop + 1
        return Batch(seqs, times, periods, values, gaps, head)

    def close(self) -> None:
        self.map.close()


def segments(directory: Path = SHM_DIR) -> list[Path]:
    try:
        return sorted(p for p in directory.iterdir() if p.name.startswith(PREFIX))
    except FileNotFoundError:
        return []
