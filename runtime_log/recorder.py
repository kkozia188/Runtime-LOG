"""Record one run: follow launch.log, sample processes/threads, poll the EtherCAT master, finalize.

All reads are passive: launch.log is tailed, /proc is read, `ethercat master|slaves` reads the
master's cached state. Nothing is sent to the robot stack.
"""

from __future__ import annotations

import gzip
import json
import queue
import shutil
import time
from pathlib import Path
from typing import Optional

from . import timeline
from .archive import RUN_FILE, Archive, read_json, run_id, write_json
from .ethercat import CounterTracker, EthercatPoller
from .launchlog import parse_line
from .meta import artifact_info, git_info, hardware_kind, host_info, launch_args, params_files, ros_overrides, thread_layout
from .model import PROCESS_STARTED, SHUTDOWN, SIGNAL, Event
from .blackbox import BlackBox
from .blackbox import Settings as BlackBoxSettings
from .procfs import CpuSampler, ProcFS
from .profile import Profile
from .sendtrace import read_send_trace
from .poststop import read_post_stop
from .poststop import summarize as summarize_post_stop
from .shm import SHM_DIR


class JsonlWriter:
    def __init__(self, path: Path):
        self.path = path
        self.stream = path.open("a", encoding="utf-8")

    def write(self, record: dict) -> None:
        self.stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        self.stream.flush()

    def close(self) -> None:
        self.stream.close()


class LogTail:
    """Incremental reader that only returns complete lines."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.partial = b""

    def read(self) -> list[str]:
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
        except FileNotFoundError:
            return []
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()
        return [line.decode("utf-8", "replace") for line in lines]


class RunRecorder:
    def __init__(
        self,
        launch_pid: int,
        log_dir: Path,
        profile: Profile,
        archive: Archive,
        proc: ProcFS,
        poller: Optional[EthercatPoller] = None,
        ethercat_hz: Optional[tuple[float, float]] = None,
        proc_hz: float = 1.0,
        clock=time.time,
        shm_dir: Path = SHM_DIR,
        blackbox_settings: Optional[BlackBoxSettings] = None,
    ):
        self.launch_pid = launch_pid
        self.shm_dir = shm_dir
        self.blackbox_settings = blackbox_settings
        self.blackboxes: dict[str, BlackBox] = {}
        self.blackbox_events: "queue.Queue[dict]" = queue.Queue()
        self.next_attach = 0.0
        self.log_dir = log_dir
        self.profile = profile
        self.archive = archive
        self.proc = proc
        self.poller = poller
        self.clock = clock
        settings = profile.ethercat
        self.ecat_startup_hz, self.ecat_steady_hz = ethercat_hz or (
            float(settings.get("startup_poll_hz", 5)),
            float(settings.get("steady_poll_hz", 1)),
        )
        self.settle_s = float(settings.get("settle_s", 5))
        self.proc_period = 1.0 / proc_hz if proc_hz > 0 else None

        cmdline = proc.cmdline(launch_pid)
        environ = proc.environ(launch_pid)
        self.args = launch_args(cmdline)
        self.domain = environ.get("ROS_DOMAIN_ID", "0")
        self.scope = self.args.get(profile.run.get("scope_arg", "control_scope"), "")
        self.start = self.clock()
        self.events: list[Event] = []
        self.tail = LogTail(log_dir / "launch.log")
        first = self._first_timestamp()
        if first is not None:
            self.start = first
        previous = archive.find_run(ros_log_dir=str(log_dir))
        self.id = previous or archive.unique_id(run_id(self.start, self.domain, self.scope))
        self.dir = archive.run_dir(self.id)
        # A service restart re-reads launch.log from the start, so events are rebuilt; samples are kept.
        (self.dir / "events.jsonl").unlink(missing_ok=True)
        self.events_out = JsonlWriter(self.dir / "events.jsonl")
        self.kernel_seen: set[tuple[float, str]] = set()
        self.resources_out = JsonlWriter(self.dir / "resources.jsonl")
        self.ecat_out: Optional[JsonlWriter] = None
        self.counters = CounterTracker()
        self.cpu = CpuSampler(proc.clk_tck)
        self.control_pid: Optional[int] = None
        generated = self.args.get(profile.run.get("artifact_arg", "generated_dir"))
        self.artifact: dict = artifact_info(Path(generated)) if generated else {}
        self.mock: Optional[bool] = None
        self.uses_ethercat = False
        self.ready_ts: Optional[float] = None
        self.next_proc = 0.0
        self.next_ecat = 0.0
        self.next_flush = 0.0
        self.last_slave_states: Optional[str] = None
        self.peak_cpu = 0.0
        self.peak_rss_kib = 0
        self.threads: dict[int, dict] = {}
        self.ended: Optional[float] = None
        if previous:
            self._restore_polled_events()
        self.document = {
            "id": self.id,
            "source": "live",
            "start": self.start,
            "end": None,
            "domain": self.domain,
            "scope": self.scope,
            "launch_pid": launch_pid,
            "launch_cmdline": cmdline,
            "launch_args": self.args,
            "ros_log_dir": str(log_dir),
            "rmw": environ.get("RMW_IMPLEMENTATION"),
            "host": host_info(proc),
            "profile": profile.name,
            "result": "running",
            "git": [git_info(repo) for repo in profile.git_repos],
            "polling": {"proc_hz": proc_hz, "ethercat_startup_hz": self.ecat_startup_hz, "ethercat_steady_hz": self.ecat_steady_hz},
        }

    # ------------------------------------------------------------------ helpers
    def _first_timestamp(self) -> Optional[float]:
        try:
            with (self.log_dir / "launch.log").open("r", encoding="utf-8", errors="replace") as stream:
                for raw in stream:
                    line = parse_line(raw)
                    if line is not None:
                        return line.ts
        except FileNotFoundError:
            return None
        return None

    def _restore_polled_events(self) -> None:
        """Service restart inside a run: rebuild polled events and continue the counter baseline."""
        path = self.dir / "ethercat.jsonl"
        if not path.exists():
            return
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        for row in rows:
            if not row.get("counters"):
                continue
            event = counter_event(row["ts"], self.counters.update(row["counters"]))
            if event is not None:
                self._emit(event)
            states = row.get("states")
            if states and states != self.last_slave_states:
                self._emit(Event(row["ts"], "ecat", "ethercat", "slaves", states, "info", rule="slave_states_polled", label="从站状态（轮询）"))
                self.last_slave_states = states

    def _emit(self, event: Event) -> None:
        self.events.append(event)
        self.events_out.write(event.to_dict(self.start))

    # ------------------------------------------------------------------ sampling
    def poll_log(self) -> None:
        for raw in self.tail.read():
            line = parse_line(raw)
            if line is None:
                continue
            for event in self.profile.classify(line):
                self._emit(event)
                if event.kind == PROCESS_STARTED and event.data.get("base") == "ros2_control_node":
                    self._attach_control_node(event.data["pid"])
                if event.rule in self.profile.ready_stages:
                    self.ready_ts = event.ts

    def _attach_control_node(self, pid: int) -> None:
        self.control_pid = pid
        cmdline = self.proc.cmdline(pid)
        suffix = self.profile.run.get("params_file_suffix", "controllers.yaml")
        for path in params_files(cmdline):
            if path.endswith(suffix) and Path(path).parent.is_dir():
                self.artifact = artifact_info(Path(path).parent)
        self.mock, self.uses_ethercat = hardware_kind(self.artifact, self.profile.run, self.args)
        trace = self.proc.environ(pid).get("RT_CONTROL_ECAT_TRACE_FILE")
        if trace:
            self.document["send_trace_path"] = trace
        self.document.update(
            {
                "control_pid": pid,
                "artifact": self.artifact,
                "parameter_overrides": ros_overrides(cmdline),
                "mock": self.mock,
                "hardware": "mock" if self.mock else ("ethercat" if self.uses_ethercat else "other"),
            }
        )
        if self.uses_ethercat and self.poller is not None and self.ecat_startup_hz > 0:
            self.ecat_out = JsonlWriter(self.dir / "ethercat.jsonl")

    def add_kernel_events(self, events: list[Event]) -> None:
        for event in events:
            key = (round(event.ts, 6), event.message)
            if key in self.kernel_seen:
                continue
            if event.ts >= self.start - 1.0 and (self.ended is None or event.ts <= self.ended + 3.0):
                self.kernel_seen.add(key)
                self._emit(event)
                if event.level == "error":
                    for box in self.blackboxes.values():
                        box.trigger("kernel_error", event.ts, event.message[:200])

    # ------------------------------------------------------------------ black box
    def attach_segments(self) -> None:
        """Start a black box for every recorder segment written by this run's control process."""
        if self.control_pid is None or self.ended is not None:
            return
        try:
            candidates = [p for p in self.shm_dir.iterdir() if p.name.startswith(f"runtime-log.{self.control_pid}.")]
        except FileNotFoundError:
            return
        for path in candidates:
            if path.name in self.blackboxes:
                continue
            try:
                box = BlackBox(path, self.dir / "blackbox", self.blackbox_settings, explain=self._explain,
                               kernel_lines=self._kernel_lines, on_event=self.blackbox_events.put)
            except (OSError, ValueError) as error:
                self.blackboxes[path.name] = None  # type: ignore[assignment]  # unreadable: do not retry
                self._emit(Event(self.clock(), "blackbox", "runtime-log", path.name, f"segment unreadable: {error}", "warn", rule="segment_unreadable", label="共享内存段不可读"))
                continue
            self.blackboxes[path.name] = box
            box.start()
            self._emit(Event(self.clock(), "blackbox", "runtime-log", path.name,
                             f"{box.reader.writer_name}: {len(box.reader.fields)} fields", "info", rule="segment_attached", label="黑匣子开始记录"))

    def _active_boxes(self) -> list[BlackBox]:
        return [box for box in self.blackboxes.values() if box is not None]

    def _explain(self, start: float, end: float) -> Optional[str]:
        """Why a state change in [start, end] is expected: a disable request or the stop sequence."""
        shutdown = next((e.ts for e in list(self.events) if e.kind in (SHUTDOWN, SIGNAL)), None)
        if shutdown is not None and end >= shutdown - 1.0:
            return "shutdown"
        if self.ended is not None and end >= self.ended - 3.0:
            return "run ended"
        for event in list(self.events):
            if event.rule == "rt_disable" and start <= event.ts <= end:
                return "/rt/disable"
        return None

    def _kernel_lines(self, start: float, end: float) -> list[str]:
        return [
            f"{time.strftime('%H:%M:%S', time.localtime(e.ts))}.{int((e.ts % 1) * 1e6):06d} {e.message}"
            for e in list(self.events)
            if e.source == "kernel" and start - 1.0 <= e.ts <= end + 1.0
        ]

    def drain_blackbox_events(self) -> None:
        while True:
            try:
                item = self.blackbox_events.get_nowait()
            except queue.Empty:
                return
            if item["kind"] == "blackbox_saved":
                self._emit(Event(item["ts"], "blackbox", "runtime-log", item["dir"], item.get("detail", ""), "error",
                                 rule="blackbox_saved", label="黑匣子触发：" + ", ".join(item["reasons"]), data={"reasons": item["reasons"], "dir": item["dir"]}))
            else:
                self._emit(Event(item["ts"], "blackbox", "runtime-log", "", ", ".join(item["reasons"]), "info",
                                 rule="blackbox_expected", label="预期内的状态切换（" + item["explained_by"] + "）",
                                 data={"reasons": item["reasons"], "explained_by": item["explained_by"]}))

    def manual_trigger(self, reason: str) -> int:
        for box in self._active_boxes():
            box.trigger("manual", None, reason)
        return len(self._active_boxes())

    def _blackbox_summary(self) -> list[dict]:
        return [box.describe() for box in self._active_boxes()]

    def sample_processes(self, now: float) -> None:
        tree = self.proc.descendants(self.launch_pid)
        rows = []
        live_keys = set()
        for pid in tree:
            stat = self.proc.stat(pid)
            if stat is None:
                continue
            key = (pid, None)
            live_keys.add(key)
            cpu = self.cpu.percent(key, now, stat["utime"] + stat["stime"])
            status = self.proc.status(pid)
            row = {
                "pid": pid,
                "comm": stat["comm"],
                "cpu": None if cpu is None else round(cpu, 2),
                "rss_kib": status.get("rss_kib", stat["rss_pages"] * 4),
                "threads": stat["num_threads"],
                "nonvol_ctxt": status.get("nonvol_ctxt"),
            }
            rows.append(row)
            if cpu is not None:
                self.peak_cpu = max(self.peak_cpu, cpu)
            self.peak_rss_kib = max(self.peak_rss_kib, row["rss_kib"] or 0)
        thread_rows = []
        if self.control_pid is not None:
            for tid in self.proc.threads(self.control_pid):
                stat = self.proc.stat(self.control_pid, tid)
                if stat is None:
                    continue
                key = (self.control_pid, tid)
                live_keys.add(key)
                cpu = self.cpu.percent(key, now, stat["utime"] + stat["stime"])
                thread_rows.append([tid, stat["comm"], stat["processor"], None if cpu is None else round(cpu, 2)])
                known = self.threads.get(tid)
                if known is None:
                    self.threads[tid] = {
                        "tid": tid,
                        "name": stat["comm"],
                        "policy": stat["policy"],
                        "rt_priority": stat["rt_priority"],
                        "first_seen_s": round(now - self.start, 3),
                        "cpus_seen": [stat["processor"]],
                        "affinity": self.proc.affinity(tid),
                        "peak_cpu": cpu or 0.0,
                    }
                else:
                    known.update({"name": stat["comm"], "policy": stat["policy"], "rt_priority": stat["rt_priority"]})
                    if stat["processor"] not in known["cpus_seen"]:
                        known["cpus_seen"].append(stat["processor"])
                    known["peak_cpu"] = max(known["peak_cpu"], cpu or 0.0)
                    if known.get("affinity") is None:
                        known["affinity"] = self.proc.affinity(tid)
        self.cpu.forget(live_keys)
        self.resources_out.write({"ts": round(now, 3), "t": round(now - self.start, 3), "procs": rows, "threads": thread_rows})

    def poll_ethercat(self, now: float) -> None:
        if self.poller is None or self.ecat_out is None:
            return
        try:
            master = self.poller.master_state()
            slaves = self.poller.slave_states()
        except Exception as error:  # the master can be absent between runs; record and continue
            self.ecat_out.write({"ts": round(now, 3), "t": round(now - self.start, 3), "error": str(error)[:200]})
            return
        delta = self.counters.update(master.get("counters", {}))
        states = " ".join(f"{s['pos']}:{s['state']}{'E' if s['error'] else ''}" for s in slaves)
        record = {
            "ts": round(now, 3),
            "t": round(now - self.start, 3),
            "phase": master.get("phase"),
            "active": master.get("active"),
            "slaves": master.get("slaves"),
            "link": master.get("link"),
            "counters": master.get("counters"),
            "delta": delta,
            "states": states,
        }
        self.ecat_out.write(record)
        if states != self.last_slave_states:
            self._emit(Event(now, "ecat", "ethercat", "slaves", states, "info", rule="slave_states_polled", label="从站状态（轮询）"))
            self.last_slave_states = states
        event = counter_event(now, delta)
        if event is not None:
            self._emit(event)

    def ecat_period(self, now: float) -> Optional[float]:
        steady = self.ready_ts is not None and now - self.ready_ts >= self.settle_s
        hz = self.ecat_steady_hz if steady else self.ecat_startup_hz
        return 1.0 / hz if hz > 0 else None

    # ------------------------------------------------------------------ loop
    def alive(self) -> bool:
        return self.proc.alive(self.launch_pid)

    def tick(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        self.poll_log()
        if self.proc_period and now >= self.next_proc:
            self.sample_processes(now)
            self.next_proc = now + self.proc_period
        period = self.ecat_period(now)
        if period and now >= self.next_ecat:
            self.poll_ethercat(now)
            self.next_ecat = now + period
        if now >= self.next_attach:
            self.attach_segments()
            self.next_attach = now + 1.0
        self.drain_blackbox_events()
        if now >= self.next_flush:
            self.flush(now)
            self.next_flush = now + 5.0

    def _summarize(self, **kwargs) -> dict:
        summary = timeline.summarize(self.events, self.profile.ready_stages, optional_processes=self.profile.run.get("optional_processes", []), **kwargs)
        blackbox = self._blackbox_summary()
        overruns = sum((box["cycle_stats"].get("overruns") or 0) for box in blackbox)
        gaps = sum(box["gaps"] for box in blackbox)
        if overruns:
            summary["anomalies"]["cycle.overruns"] = overruns
        if gaps:
            summary["anomalies"]["blackbox.reader_gaps"] = gaps
        trace = self.document.get("send_trace") or {}
        burst = sum(phase.get("gaps_below_burst", 0) for phase in (trace.get("phases") or {}).values())
        if burst:
            summary["anomalies"]["ecat.send_burst"] = burst
        expected_codes = (self.profile.ethercat.get("post_stop") or {}).get("expected_codes") or []
        summary["anomalies"].update(summarize_post_stop(self.document.get("post_stop"), expected_codes))
        summary["anomalies"] = dict(sorted(summary["anomalies"].items()))
        summary["anomaly_total"] = sum(summary["anomalies"].values())
        self.document["blackbox"] = blackbox
        return summary

    def flush(self, now: float) -> None:
        for writer in (self.events_out, self.resources_out, self.ecat_out):
            if writer is not None:
                writer.flush()
        self.document["summary"] = self._summarize(running=True, end_ts=now)
        self.document["resources"] = self._resource_summary()
        write_json(self.dir / RUN_FILE, self.document)

    def _resource_summary(self) -> dict:
        return {
            "peak_cpu_percent": round(self.peak_cpu, 1),
            "peak_rss_kib": self.peak_rss_kib,
            "threads": sorted(self.threads.values(), key=lambda row: (-row["rt_priority"], row["tid"])),
        }

    def stop(self, now: Optional[float] = None) -> None:
        """The launch process is gone: stop sampling; kernel events are still accepted briefly."""
        if self.ended is None:
            self.ended = self.clock() if now is None else now
        self.poll_log()

    def finish(self, now: Optional[float] = None, interrupted: bool = False) -> dict:
        self.stop(now)
        boxes = self._active_boxes()
        for box in boxes:
            box.stop()
        for box in boxes:
            box.join(timeout=15.0)
        self.drain_blackbox_events()
        for box in boxes:
            # A finished writer's segment is no longer needed once read; a live one (service restart) stays.
            if not box.reader.writer_alive():
                box.reader.close()
                box.path.unlink(missing_ok=True)
        if self.document.get("send_trace_path"):
            self.document["send_trace"] = read_send_trace(Path(self.document["send_trace_path"]))
        post_stop = self.profile.ethercat.get("post_stop") or {}
        if not interrupted and self.uses_ethercat and not self.mock and self.poller is not None and post_stop.get("enabled"):
            result = read_post_stop(self.poller, post_stop)
            self.document["post_stop"] = result
            for row in result.get("drives", []):
                if row.get("error_code") or row.get("fault"):
                    self._emit(Event(result["time"], "ecat", "ethercat", f"slave {row['position']}",
                                     f"0x603F={row.get('error_code_hex')} 0x6041={row.get('status_word_hex')} {row.get('error_text', '')}".strip(),
                                     "warn", rule="post_stop_error_code", label="停后驱动错误码"))
            if result.get("skipped"):
                self._emit(Event(result["time"], "ecat", "ethercat", "master", result["skipped"], "info",
                                 rule="post_stop_skipped", label="停后错误码未读"))
        if self.tail.partial:
            line = parse_line(self.tail.partial.decode("utf-8", "replace"))
            if line is not None:
                for event in self.profile.classify(line):
                    self._emit(event)
        summary = self._summarize(running=interrupted, end_ts=None if not interrupted else self.ended)
        result = "interrupted" if interrupted and summary["result"] == "running" else summary["result"]
        summary["result"] = result
        self.document.update(
            {
                "end": summary["end"],
                "result": result,
                "summary": summary,
                "resources": self._resource_summary(),
            }
        )
        for writer in (self.events_out, self.resources_out, self.ecat_out):
            if writer is not None:
                writer.close()
        _copy_gzip(self.log_dir / "launch.log", self.dir / "launch.log.gz")
        write_json(self.dir / RUN_FILE, self.document)
        return self.document


def counter_event(ts: float, delta: dict) -> Optional[Event]:
    if not (delta.get("lost_frames") or delta.get("tx_errors")):
        return None
    return Event(
        ts, "ecat", "ethercat", "master", f"lost_frames +{delta.get('lost_frames', 0)}, tx_errors +{delta.get('tx_errors', 0)}",
        "warn", rule="counter_increase", label="主站计数增加", data={k: v for k, v in delta.items() if k in ("lost_frames", "tx_errors")},
    )


def counter_events(ethercat_rows: list[dict]) -> list[Event]:
    """Rebuild counter events from archived `ethercat.jsonl` rows (replay)."""
    tracker = CounterTracker()
    events = []
    for row in ethercat_rows:
        if row.get("counters"):
            event = counter_event(row["ts"], tracker.update(row["counters"]))
            if event is not None:
                events.append(event)
    return events


def _copy_gzip(source: Path, target: Path) -> None:
    try:
        with source.open("rb") as input_stream, gzip.open(target, "wb", compresslevel=6) as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
    except FileNotFoundError:
        pass


def load_run(directory: Path) -> tuple[dict, list[Event]]:
    document = read_json(directory / RUN_FILE, {}) or {}
    events = []
    path = directory / "events.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(Event.from_dict(json.loads(line)))
    return document, events
