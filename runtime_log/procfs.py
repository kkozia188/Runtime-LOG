"""Read-only /proc access: process tree, per-process and per-thread CPU/memory/placement.

Every function takes the proc root so tests can run against a fixture tree. Nothing here writes
to /proc or changes any process: the only affinity call is sched_getaffinity (read).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

POLICIES = {0: "OTHER", 1: "FIFO", 2: "RR", 3: "BATCH", 5: "IDLE", 6: "DEADLINE"}


def parse_stat(text: str) -> dict:
    """`/proc/<pid>/stat`; comm may contain spaces and parentheses, so split at the last ')'."""
    start = text.index("(")
    end = text.rindex(")")
    comm = text[start + 1 : end]
    rest = text[end + 2 :].split()
    # rest[0] is field 3 (state) in proc(5) numbering.
    return {
        "pid": int(text[:start].strip()),
        "comm": comm,
        "state": rest[0],
        "ppid": int(rest[1]),
        "utime": int(rest[11]),
        "stime": int(rest[12]),
        "priority": int(rest[15]),
        "nice": int(rest[16]),
        "num_threads": int(rest[17]),
        "starttime": int(rest[19]),
        "rss_pages": int(rest[21]),
        "processor": int(rest[36]),
        "rt_priority": int(rest[37]),
        "policy": POLICIES.get(int(rest[38]), str(rest[38])),
    }


def parse_status(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        value = value.strip()
        if key == "VmRSS":
            result["rss_kib"] = int(value.split()[0])
        elif key == "voluntary_ctxt_switches":
            result["vol_ctxt"] = int(value)
        elif key == "nonvoluntary_ctxt_switches":
            result["nonvol_ctxt"] = int(value)
    return result


def parse_btime(text: str) -> int:
    for line in text.splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    raise ValueError("btime missing from /proc/stat")


def parse_cpu_list(text: str) -> set[int]:
    cpus: set[int] = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            low, high = part.split("-", 1)
            cpus.update(range(int(low), int(high) + 1))
        else:
            cpus.add(int(part))
    return cpus


class ProcFS:
    def __init__(self, root: str | Path = "/proc"):
        self.root = Path(root)
        self.live = str(self.root) == "/proc"
        self.clk_tck = os.sysconf("SC_CLK_TCK") if self.live else 100

    def _read(self, *parts) -> Optional[str]:
        try:
            return (self.root.joinpath(*map(str, parts))).read_text(errors="replace")
        except (FileNotFoundError, ProcessLookupError, PermissionError, NotADirectoryError):
            return None

    def boot_time(self) -> int:
        return parse_btime(self._read("stat") or "")

    def pids(self) -> list[int]:
        try:
            return [int(entry.name) for entry in self.root.iterdir() if entry.name.isdigit()]
        except FileNotFoundError:
            return []

    def stat(self, pid: int, tid: Optional[int] = None) -> Optional[dict]:
        text = self._read(pid, "task", tid, "stat") if tid is not None else self._read(pid, "stat")
        if not text:
            return None
        try:
            return parse_stat(text)
        except (ValueError, IndexError):
            return None

    def status(self, pid: int) -> dict:
        text = self._read(pid, "status")
        return parse_status(text) if text else {}

    def cmdline(self, pid: int) -> list[str]:
        raw = None
        try:
            raw = self.root.joinpath(str(pid), "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
        if raw is None:  # fixture trees store a readable copy
            text = self._read(pid, "cmdline.txt")
            return text.split() if text else []
        return [part.decode(errors="replace") for part in raw.split(b"\0") if part]

    def environ(self, pid: int) -> dict:
        try:
            raw = self.root.joinpath(str(pid), "environ").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return {}
        result = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.decode(errors="replace").partition("=")
            if sep:
                result[key] = value
        return result

    def io(self, pid: int) -> dict:
        text = self._read(pid, "io")
        result = {}
        for line in (text or "").splitlines():
            key, _, value = line.partition(":")
            if key in ("read_bytes", "write_bytes"):
                result[key] = int(value)
        return result

    def threads(self, pid: int) -> list[int]:
        try:
            return sorted(int(entry.name) for entry in self.root.joinpath(str(pid), "task").iterdir() if entry.name.isdigit())
        except (FileNotFoundError, NotADirectoryError):
            return []

    def thread_name(self, pid: int, tid: int) -> str:
        stat = self.stat(pid, tid)
        return stat["comm"] if stat else ""

    def affinity(self, tid: int) -> Optional[str]:
        """Allowed CPU list (read-only). Only meaningful on the live /proc."""
        if not self.live:
            return None
        try:
            return format_cpu_list(os.sched_getaffinity(tid))
        except (ProcessLookupError, PermissionError, OSError):
            return None

    def alive(self, pid: int) -> bool:
        stat = self.stat(pid)
        return stat is not None and stat["state"] not in ("Z", "X")

    def descendants(self, root_pid: int, include_root: bool = True) -> list[int]:
        children: dict[int, list[int]] = {}
        for pid in self.pids():
            stat = self.stat(pid)
            if stat:
                children.setdefault(stat["ppid"], []).append(pid)
        result, pending = [], [root_pid]
        seen = set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if pid != root_pid or include_root:
                result.append(pid)
            pending.extend(children.get(pid, []))
        return sorted(p for p in result if self.stat(p) is not None)

    def isolated_cpus(self, sys_root: str | Path = "/sys") -> set[int]:
        try:
            return parse_cpu_list(Path(sys_root, "devices/system/cpu/isolated").read_text())
        except FileNotFoundError:
            return set()


def format_cpu_list(cpus: Iterable[int]) -> str:
    values = sorted(set(cpus))
    parts, start, prev = [], None, None
    for cpu in values:
        if start is None:
            start = prev = cpu
        elif cpu == prev + 1:
            prev = cpu
        else:
            parts.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = cpu
    if start is not None:
        parts.append(f"{start}-{prev}" if prev != start else str(start))
    return ",".join(parts)


class CpuSampler:
    """CPU% per (pid, tid) between consecutive samples, from utime+stime ticks."""

    def __init__(self, clk_tck: int):
        self.clk_tck = clk_tck
        self.previous: dict[tuple, tuple[float, int]] = {}

    def percent(self, key: tuple, now: float, ticks: int) -> Optional[float]:
        before = self.previous.get(key)
        self.previous[key] = (now, ticks)
        if before is None:
            return None
        elapsed = now - before[0]
        if elapsed <= 0:
            return None
        return max(0.0, (ticks - before[1]) / self.clk_tck / elapsed * 100.0)

    def forget(self, live_keys: set[tuple]) -> None:
        for key in list(self.previous):
            if key not in live_keys:
                del self.previous[key]
