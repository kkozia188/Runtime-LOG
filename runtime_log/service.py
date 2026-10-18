"""Service loop: discover runs, feed kernel events, drive recorders, retention, index updates."""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import selectors
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from . import kernlog
from .archive import Archive
from .ethercat import EthercatPoller
from .model import Event
from .procfs import ProcFS
from .profile import Profile
from .recorder import RunRecorder

LOG = logging.getLogger("runtime_log")


class KernelFollower:
    """`journalctl -k -f -o json` reader; keeps the last minute of EtherCAT events for new runs."""

    def __init__(self, command: Optional[list[str]] = None, keep_s: float = 60.0):
        self.command = command or ["journalctl", "-k", "-f", "-o", "json", "--since", "now", "--no-pager"]
        self.keep_s = keep_s
        self.process: Optional[subprocess.Popen] = None
        self.selector = selectors.DefaultSelector()
        self.partial = b""
        self.recent: collections.deque[Event] = collections.deque()

    def start(self) -> None:
        self.process = subprocess.Popen(self.command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.selector.register(self.process.stdout, selectors.EVENT_READ)

    def read(self) -> list[Event]:
        if self.process is None:
            return []
        if self.process.poll() is not None:
            LOG.warning("journalctl exited (%s); restarting", self.process.returncode)
            self.selector.unregister(self.process.stdout)
            self.start()
            return []
        events = []
        while self.selector.select(timeout=0):
            try:
                chunk = self.process.stdout.read()
            except BlockingIOError:
                break
            if not chunk:
                break
            data = self.partial + chunk
            lines = data.split(b"\n")
            self.partial = lines.pop()
            for line in lines:
                if b"EtherCAT" not in line:
                    continue
                try:
                    event = kernlog.parse_entry(json.loads(line))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
                if event is not None:
                    events.append(event)
                    self.recent.append(event)
        cutoff = time.time() - self.keep_s
        while self.recent and self.recent[0].ts < cutoff:
            self.recent.popleft()
        return events

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()


def kernel_history(since: float, until: Optional[float] = None) -> list[Event]:
    """EtherCAT kernel events in a past window (one journalctl call)."""
    command = ["journalctl", "-k", "-o", "json", "--no-pager", "--since", f"@{since:.0f}"]
    if until is not None:
        command += ["--until", f"@{until + 1:.0f}"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    events = []
    for event in kernlog.parse_lines(completed.stdout.splitlines()):
        if event.ts >= since and (until is None or event.ts <= until):
            events.append(event)
    return events


def find_log_dir(log_root: Path, pid: int) -> Optional[Path]:
    suffix = f"-{pid}"
    best = None
    try:
        with os.scandir(log_root) as entries:
            for entry in entries:
                if entry.name.endswith(suffix) and entry.name[:2] == "20" and entry.is_dir():
                    if best is None or entry.name > best.name:
                        best = entry
    except FileNotFoundError:
        return None
    return Path(best.path) if best else None


class Discovery:
    """Find `ros2 launch` processes that match the profile; remembers pids already judged."""

    def __init__(self, proc: ProcFS, pattern: str):
        self.proc = proc
        self.pattern = re.compile(pattern)
        self.judged: dict[int, tuple[int, bool]] = {}  # pid -> (starttime, matches)

    def scan(self) -> list[int]:
        found = []
        live = set()
        for pid in self.proc.pids():
            live.add(pid)
            stat = self.proc.stat(pid)
            if stat is None:
                continue
            cached = self.judged.get(pid)
            if cached and cached[0] == stat["starttime"]:
                if cached[1]:
                    found.append(pid)
                continue
            if stat["comm"] not in ("ros2", "python3", "python3.10", "python"):
                self.judged[pid] = (stat["starttime"], False)
                continue
            matches = bool(self.pattern.search(" ".join(self.proc.cmdline(pid))))
            self.judged[pid] = (stat["starttime"], matches)
            if matches:
                found.append(pid)
        for pid in list(self.judged):
            if pid not in live:
                del self.judged[pid]
        return found


def log_root_for(proc: ProcFS, pid: int) -> Path:
    environ = proc.environ(pid)
    if environ.get("ROS_LOG_DIR"):
        return Path(environ["ROS_LOG_DIR"])
    home = environ.get("ROS_HOME") or os.path.join(environ.get("HOME", str(Path.home())), ".ros")
    return Path(home) / "log"


class Service:
    def __init__(
        self,
        profile: Profile,
        archive: Archive,
        proc: Optional[ProcFS] = None,
        poller: Optional[EthercatPoller] = None,
        ethercat_hz: Optional[tuple[float, float]] = None,
        proc_hz: float = 1.0,
        tick_s: float = 0.2,
        discovery_s: float = 1.0,
        drain_s: float = 3.0,
        on_finish: Optional[Callable[[dict], None]] = None,
    ):
        self.profile = profile
        self.archive = archive
        self.proc = proc or ProcFS()
        self.poller = poller
        self.ethercat_hz = ethercat_hz
        self.proc_hz = proc_hz
        self.tick_s = tick_s
        self.discovery_s = discovery_s
        self.drain_s = drain_s
        self.on_finish = on_finish
        self.discovery = Discovery(self.proc, profile.run.get("launch_match", r"ros2 launch "))
        self.kernel = KernelFollower()
        self.recorders: dict[int, RunRecorder] = {}
        self.draining: dict[int, tuple[RunRecorder, float]] = {}
        self.pending: dict[int, float] = {}  # launch pid -> first seen, until its log dir appears
        self.running = True
        self.next_index = 0.0

    def _start_run(self, pid: int, now: float) -> None:
        log_dir = find_log_dir(log_root_for(self.proc, pid), pid)
        if log_dir is None:
            first = self.pending.setdefault(pid, now)
            if now - first > 30:
                LOG.warning("no ROS log dir for launch pid %s; ignoring it", pid)
                self.pending[pid] = float("inf")
            return
        self.pending.pop(pid, None)
        recorder = RunRecorder(pid, log_dir, self.profile, self.archive, self.proc, self.poller, self.ethercat_hz, self.proc_hz)
        recorder.add_kernel_events(kernel_history(recorder.start - 1.0))
        recorder.add_kernel_events(list(self.kernel.recent))
        self.recorders[pid] = recorder
        LOG.info("run %s started (launch pid %s, log %s)", recorder.id, pid, log_dir)
        self.archive.update_index()

    def manual_triggers(self) -> None:
        """`runtime-log trigger` drops files into <archive>/../control; each one triggers every black box."""
        control = self.archive.root.parent / "control"
        try:
            files = sorted(control.glob("trigger-*"))
        except OSError:
            return
        for path in files:
            try:
                reason = path.read_text(encoding="utf-8", errors="replace").strip()[:200] or "manual"
            finally:
                path.unlink(missing_ok=True)
            count = sum(recorder.manual_trigger(reason) for recorder in self.recorders.values())
            LOG.info("manual trigger '%s' sent to %d black boxes", reason, count)
        try:
            requests = sorted(control.glob("experiment-*"))
        except OSError:
            return
        for path in requests:
            try:
                name = path.read_text(encoding="utf-8", errors="replace").strip()[:80]
            finally:
                path.unlink(missing_ok=True)
            action = "start" if path.name.startswith("experiment-start-") else "stop"
            count = sum(recorder.manual_experiment(action, name) for recorder in self.recorders.values())
            LOG.info("experiment %s '%s' sent to %d recorders", action, name, count)

    def step(self, now: float, discover: bool) -> None:
        kernel_events = self.kernel.read()
        if discover:
            self.manual_triggers()
            for pid in self.discovery.scan():
                if pid not in self.recorders and pid not in self.draining and self.pending.get(pid) != float("inf"):
                    self._start_run(pid, now)
        for recorder in list(self.recorders.values()) + [item[0] for item in self.draining.values()]:
            recorder.add_kernel_events(kernel_events)
        for pid, recorder in list(self.recorders.items()):
            if not recorder.alive():
                recorder.stop(now)
                self.draining[pid] = (recorder, now + self.drain_s)
                del self.recorders[pid]
                continue
            try:
                recorder.tick(now)
            except Exception:  # one broken sample must not end the recording
                LOG.exception("tick failed for run %s", recorder.id)
        if self.recorders and now >= self.next_index:
            self.archive.update_index()  # keeps the live rows of the index page current
            self.next_index = now + 5.0
        for pid, (recorder, deadline) in list(self.draining.items()):
            if now >= deadline:
                del self.draining[pid]
                document = recorder.finish(now)
                LOG.info("run %s finished: %s", recorder.id, document["result"])
                self.archive.update_index()
                if self.on_finish:
                    self.on_finish(document)

    def gc(self) -> None:
        active = [r.id for r in self.recorders.values()] + [r.id for r, _ in self.draining.values()]
        removed = self.archive.collect_garbage(active=active)
        if removed:
            LOG.info("retention removed %d runs: %s", len(removed), ", ".join(removed))

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, "running", False))
        self.kernel.start()
        self.archive.update_index()
        next_discovery = next_gc = 0.0
        try:
            while self.running:
                now = time.time()
                discover = now >= next_discovery
                if discover:
                    next_discovery = now + self.discovery_s
                self.step(now, discover)
                if now >= next_gc:
                    self.gc()
                    next_gc = now + 3600.0
                time.sleep(self.tick_s)
        finally:
            now = time.time()
            for recorder in list(self.recorders.values()) + [item[0] for item in self.draining.values()]:
                recorder.finish(now, interrupted=recorder.ended is None)
            self.kernel.stop()
            self.archive.update_index()
