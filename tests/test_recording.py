"""Recorder, service loop, archive retention, offline import/replay and the read-only web view."""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from runtime_log import ethercat
from runtime_log.archive import Archive, read_json, write_json
from runtime_log.offline import import_launch_dir, replay
from runtime_log.recorder import RunRecorder, load_run
from runtime_log.report import render
from runtime_log.service import Discovery, Service, find_log_dir
from runtime_log.web import serve

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LAUNCH_PID = 235346
CONTROL_PID = 235371


def stat_line(pid, comm, ppid, utime=0, processor=3, rt_priority=0, policy=0, threads=1):
    fields = ["S", ppid, pid, pid, 0, -1, 0, 0, 0, 0, 0, utime, 0, 0, 0, 20, 0, threads, 0, 1000, 100, 2000]
    fields += [0] * 14 + [processor, rt_priority, policy, 0, 0, 0]
    return f"{pid} ({comm}) " + " ".join(map(str, fields))


class FakeProc:
    clk_tck = 100
    live = False

    def __init__(self, artifact_dir: Path):
        self.ticks = 0
        self.alive_launch = True
        self.artifact_dir = artifact_dir

    def cmdline(self, pid):
        if pid == LAUNCH_PID:
            return ["/usr/bin/python3", "/opt/ros/humble/bin/ros2", "launch", "rt_control_bringup", "rt_control_scope_runtime.launch.py",
                    "control_scope:=bench_left_arm", f"generated_dir:={self.artifact_dir}", "use_mock_hardware:=false"]
        if pid == CONTROL_PID:
            return ["/opt/ros/humble/lib/controller_manager/ros2_control_node", "--ros-args", "--params-file",
                    str(self.artifact_dir / "controllers.yaml"), "-p", "update_rate:=1000"]
        return ["bash"]

    def environ(self, pid):
        return {"ROS_DOMAIN_ID": "77", "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp", "HOME": "/home/ar"}

    def pids(self):
        return [1, LAUNCH_PID, CONTROL_PID] if self.alive_launch else [1]

    def stat(self, pid, tid=None):
        if not self.alive_launch and pid != 1:
            return None
        if pid == LAUNCH_PID:
            return _parse(stat_line(LAUNCH_PID, "ros2", 1, utime=self.ticks // 10))
        if pid == CONTROL_PID:
            if tid == CONTROL_PID + 5:
                return _parse(stat_line(tid, "ros2_control_no", LAUNCH_PID, utime=self.ticks, processor=14, rt_priority=80, policy=1))
            return _parse(stat_line(tid or pid, "ros2_control_no", LAUNCH_PID, utime=self.ticks * 2, threads=2))
        if pid == 1:
            return _parse(stat_line(1, "systemd", 0))
        return None

    def status(self, pid):
        return {"rss_kib": 51200, "nonvol_ctxt": 3}

    def threads(self, pid):
        return [CONTROL_PID, CONTROL_PID + 5] if pid == CONTROL_PID and self.alive_launch else []

    def affinity(self, tid):
        return "14" if tid == CONTROL_PID + 5 else "0-13,15-27"

    def alive(self, pid):
        return self.alive_launch

    def descendants(self, root, include_root=True):
        return [LAUNCH_PID, CONTROL_PID] if self.alive_launch else []

    def isolated_cpus(self):
        return {14}


def _parse(line):
    from runtime_log.procfs import parse_stat

    return parse_stat(line)


@pytest.fixture
def artifact_dir(tmp_path):
    directory = tmp_path / "artifact"
    directory.mkdir()
    (directory / "artifact.yaml").write_text("selection:\n  machine_unit: Alfa_V3-bench-01\n  control_scope: bench_left_arm\n")
    (directory / "controllers.yaml").write_text(
        "controller_manager:\n  ros__parameters:\n    update_rate: 1000\n    left_arm_jtc:\n      type: joint_trajectory_controller/JointTrajectoryController\n"
    )
    (directory / "robot_description.xml").write_text(
        '<robot name="r"><ros2_control name="ecat_arms" type="system"><hardware><plugin>ethercat_driver/EthercatDriver</plugin></hardware>'
        '<joint name="j1"/><joint name="j2"/></ros2_control></robot>'
    )
    return directory


def fake_poller():
    master = (FIXTURES / "ethercat_master_op.txt").read_text()
    slaves = (FIXTURES / "ethercat_slaves_op.txt").read_text()
    calls = []

    def runner(argv):
        calls.append(argv[1])
        return master if argv[1] == "master" else slaves

    return ethercat.EthercatPoller(runner=runner), calls


def test_recorder_follows_a_growing_launch_log(tmp_path, profile, artifact_dir):
    log_dir = tmp_path / "log" / f"2026-10-18-14-47-48-126798-ar-Default-string-{LAUNCH_PID}"
    log_dir.mkdir(parents=True)
    lines = (FIXTURES / "launch_normal.log").read_text().splitlines(keepends=True)
    log = log_dir / "launch.log"
    log.write_text("".join(lines[:40]) + lines[40][:20])  # ends in a partial line
    proc = FakeProc(artifact_dir)
    poller, calls = fake_poller()
    archive = Archive(tmp_path / "runs")
    recorder = RunRecorder(LAUNCH_PID, log_dir, profile, archive, proc, poller)
    t0 = recorder.start
    assert recorder.scope == "bench_left_arm" and recorder.domain == "77"
    for step in range(12):
        proc.ticks += 50
        recorder.tick(t0 + 1 + step * 0.2)
    assert recorder.control_pid == CONTROL_PID and recorder.uses_ethercat and recorder.mock is False
    assert 9 <= calls.count("master") <= 12  # 5 Hz during startup: one call per 0.2 s tick
    with log.open("a") as stream:
        stream.write(lines[40][20:] + "".join(lines[41:]))
    recorder.tick(t0 + 5)
    proc.alive_launch = False
    document = recorder.finish(t0 + 6)
    assert document["result"] == "ok"
    assert document["artifact"]["update_rate"] == 1000
    assert document["artifact"]["hardware"][0]["plugin"] == "ethercat_driver/EthercatDriver"
    assert document["parameter_overrides"] == {"update_rate": "1000"}
    threads = {t["tid"]: t for t in document["resources"]["threads"]}
    assert threads[CONTROL_PID + 5]["policy"] == "FIFO" and threads[CONTROL_PID + 5]["affinity"] == "14"
    assert document["resources"]["peak_cpu_percent"] > 0
    stored, events = load_run(archive.root / document["id"])
    assert stored["result"] == "ok" and any(e.rule == "slave_states_polled" for e in events)
    ecat_rows = [json.loads(line) for line in (archive.root / document["id"] / "ethercat.jsonl").read_text().splitlines()]
    assert ecat_rows[1]["delta"]["lost_frames"] == 0
    assert (archive.root / document["id"] / "launch.log.gz").exists()
    page = render(stored, events, archive.root / document["id"])
    assert "正常结束" in page and "从站 8" in page and "ros2_control_no" in page


def test_ethercat_polling_slows_down_after_ready_and_can_be_disabled(tmp_path, profile, artifact_dir):
    log_dir = tmp_path / f"x-{LAUNCH_PID}"
    log_dir.mkdir()
    (log_dir / "launch.log").write_text((FIXTURES / "launch_normal.log").read_text())
    proc = FakeProc(artifact_dir)
    poller, calls = fake_poller()
    recorder = RunRecorder(LAUNCH_PID, log_dir, profile, Archive(tmp_path / "runs"), proc, poller)
    recorder.poll_log()
    ready = recorder.ready_ts
    assert recorder.ecat_period(ready + 1) == pytest.approx(0.2)
    assert recorder.ecat_period(ready + 6) == pytest.approx(1.0)
    off = RunRecorder(LAUNCH_PID, log_dir, profile, Archive(tmp_path / "runs2"), proc, poller, ethercat_hz=(0, 0))
    off.tick(off.start + 1)
    assert off.ecat_period(off.start + 1) is None and off.ecat_out is None


def test_service_discovers_records_and_finishes_a_run(tmp_path, profile, artifact_dir, monkeypatch):
    log_root = tmp_path / "home/.ros/log"
    log_dir = log_root / f"2026-10-18-14-47-48-126798-ar-Default-string-{LAUNCH_PID}"
    log_dir.mkdir(parents=True)
    (log_dir / "launch.log").write_text((FIXTURES / "launch_normal.log").read_text())
    proc = FakeProc(artifact_dir)
    monkeypatch.setattr(proc, "environ", lambda pid: {"ROS_DOMAIN_ID": "77", "ROS_LOG_DIR": str(log_root)})
    monkeypatch.setattr("runtime_log.service.kernel_history", lambda since, until=None: [])
    finished = []
    archive = Archive(tmp_path / "runs")
    service = Service(profile, archive, proc=proc, poller=fake_poller()[0], drain_s=1.0, on_finish=finished.append)
    now = time.time()
    service.step(now, discover=True)
    assert len(service.recorders) == 1
    service.step(now + 1, discover=True)
    assert read_json(archive.root / "index.json")["runs"][0]["result"] == "running"
    proc.alive_launch = False
    service.step(now + 2, discover=True)
    assert not service.recorders and service.draining
    service.step(now + 3.5, discover=False)
    assert finished and finished[0]["result"] == "ok" and finished[0]["domain"] == "77"
    assert read_json(archive.root / "index.json")["runs"][0]["result"] == "ok"


def test_discovery_matches_only_rt_control_launches(tmp_path, profile, artifact_dir):
    proc = FakeProc(artifact_dir)
    discovery = Discovery(proc, profile.run["launch_match"])
    assert discovery.scan() == [LAUNCH_PID]
    assert discovery.scan() == [LAUNCH_PID]  # cached
    proc.cmdline = lambda pid: ["/usr/bin/python3", "/opt/ros/humble/bin/ros2", "launch", "other_pkg", "x.launch.py"]
    assert Discovery(proc, profile.run["launch_match"]).scan() == []


def test_service_restart_reuses_the_run_and_keeps_polled_events(tmp_path, profile, artifact_dir):
    log_dir = tmp_path / f"2026-10-18-14-47-48-1-ar-Default-string-{LAUNCH_PID}"
    log_dir.mkdir()
    (log_dir / "launch.log").write_text((FIXTURES / "launch_normal.log").read_text())
    archive = Archive(tmp_path / "runs")
    proc = FakeProc(artifact_dir)
    first = RunRecorder(LAUNCH_PID, log_dir, profile, archive, proc, fake_poller()[0])
    first.tick(first.start + 1)
    first.tick(first.start + 2)
    first.flush(first.start + 2)
    first.ecat_out.close()
    second = RunRecorder(LAUNCH_PID, log_dir, profile, archive, proc, fake_poller()[0])
    assert second.id == first.id
    assert [e.rule for e in second.events].count("slave_states_polled") == 1
    assert second.counters.high["tx_frames"] == 108821609


def test_recorder_attaches_the_control_process_segment_and_reports_blackbox_events(tmp_path, profile, artifact_dir):
    pytest.importorskip("mcap")
    from test_blackbox import FIELDS, PyWriter, run_cycles

    shm = tmp_path / "shm"
    shm.mkdir()
    log_dir = tmp_path / f"x-{LAUNCH_PID}"
    log_dir.mkdir()
    (log_dir / "launch.log").write_text((FIXTURES / "launch_normal.log").read_text().split("Stopping")[0])
    writer = PyWriter(shm / f"runtime-log.{CONTROL_PID}.runtime_log_recorder", FIELDS)
    PyWriter(shm / "runtime-log.999999.other_stack", FIELDS)  # another process: must be ignored
    recorder = RunRecorder(LAUNCH_PID, log_dir, profile, Archive(tmp_path / "runs"), FakeProc(artifact_dir), None,
                           shm_dir=shm, blackbox_settings=__import__("runtime_log.blackbox", fromlist=["Settings"]).Settings(pre_s=1, post_s=0.2, stall_s=3600))
    recorder.tick(recorder.start + 1)
    assert list(recorder.blackboxes) == [f"runtime-log.{CONTROL_PID}.runtime_log_recorder"]
    run_cycles(writer, 1500)
    run_cycles(writer, 400, sw=0x0218)
    box = recorder.blackboxes[f"runtime-log.{CONTROL_PID}.runtime_log_recorder"]
    box.stop()
    box.join(10)
    recorder.tick(recorder.start + 2)
    saved = [e for e in recorder.events if e.rule == "blackbox_saved"]
    assert saved and "drive_fault" in saved[0].data["reasons"]
    document = recorder.finish(recorder.start + 3)
    assert document["summary"]["anomalies"]["blackbox.drive_fault"] == 1
    assert document["blackbox"][0]["cycle_stats"]["cycles"] == 1900
    assert (shm / f"runtime-log.{CONTROL_PID}.runtime_log_recorder").exists()  # writer (this pid) still alive
    page = render(document, recorder.events, recorder.dir)
    assert "1 kHz 黑匣子" in page and "window.mcap" in page


def test_console_audit_lines_of_this_domain_join_the_timeline(tmp_path, profile, artifact_dir, monkeypatch):
    home = tmp_path / "home"
    audit = home / ".ros/log/rt_control_arm_console/audit.jsonl"
    audit.parent.mkdir(parents=True)
    log_dir = tmp_path / f"x-{LAUNCH_PID}"
    log_dir.mkdir()
    lines = (FIXTURES / "launch_normal.log").read_text()
    (log_dir / "launch.log").write_text(lines)
    start = float(lines.split(" ", 1)[0])
    proc = FakeProc(artifact_dir)
    monkeypatch.setattr(proc, "environ", lambda pid: {"ROS_DOMAIN_ID": "77", "HOME": str(home)})
    entries = [
        {"ts": start - 100, "domain": "77", "action": "login", "ok": True},  # before the run
        {"ts": start + 10, "domain": "91", "action": "enable", "args": {"enabled": True}, "ok": True},  # other stack
        {"ts": start + 20, "domain": "77", "action": "enable", "args": {"enabled": True}, "ok": True, "session": "ab12cd34"},
        {"ts": start + 30, "domain": "77", "action": "goto_pose", "args": {"name": "home", "gear": "low"}, "ok": False,
         "error": "not_enabled", "message": "请先使能"},
    ]
    audit.write_text("".join(json.dumps(e) + "\n" for e in entries[:3]))
    recorder = RunRecorder(LAUNCH_PID, log_dir, profile, Archive(tmp_path / "runs"), proc, None)
    recorder.tick(recorder.start + 1)
    with audit.open("a") as stream:
        stream.write(json.dumps(entries[3]) + "\n")
    recorder.tick(recorder.start + 3)
    operator = [e for e in recorder.events if e.kind == "operator"]
    assert [(e.label, e.level) for e in operator] == [("操作台使能", "info"), ("去位姿", "warn")]
    assert "会话 ab12cd34" in operator[0].message and "请先使能" in operator[1].message
    document = recorder.finish(recorder.start + 4)
    assert document["summary"]["anomalies"]["operator.rejected"] == 1


def test_counter_events_from_archived_rows_ignore_in_flight_dips():
    from runtime_log.recorder import counter_events

    rows = [{"ts": float(i), "counters": {"tx_frames": 1000 * (i + 1), "lost_frames": lost, "tx_errors": 732}}
            for i, lost in enumerate([541, 541, 540, 541, 541, 545])]
    events = counter_events(rows)
    assert [(e.ts, e.data) for e in events] == [(5.0, {"lost_frames": 4, "tx_errors": 0})]


def test_find_log_dir_picks_the_directory_with_the_pid_suffix(tmp_path):
    (tmp_path / "2026-10-18-14-47-48-1-host-1234").mkdir()
    (tmp_path / "2026-10-18-14-47-48-1-host-11234").mkdir()
    (tmp_path / "python3_1234_1.log").write_text("")
    assert find_log_dir(tmp_path, 1234).name == "2026-10-18-14-47-48-1-host-1234"
    assert find_log_dir(tmp_path, 99) is None


def test_retention_and_disk_guard(tmp_path, monkeypatch):
    archive = Archive(tmp_path / "runs", retention_days=7)
    now = 1_800_000_000.0
    for name, age_days in (("old", 8), ("recent", 1), ("live", 30)):
        write_json(archive.root / name / "run.json", {"id": name, "start": now - age_days * 86400, "end": now - age_days * 86400,
                                                       "result": "running" if name == "live" else "ok"})
    assert archive.collect_garbage(now=now) == ["old"]
    assert sorted(p.name for p in archive.root.iterdir() if p.is_dir()) == ["live", "recent"]

    from collections import namedtuple

    usage = namedtuple("usage", "total used free")(100, 99, 1)
    monkeypatch.setattr("runtime_log.archive.shutil.disk_usage", lambda path: usage)
    assert archive.collect_garbage(now=now) == ["recent"]  # low disk: oldest finished run goes, live run stays


def test_import_and_replay_of_a_failed_run(tmp_path, profile):
    log_dir = tmp_path / "2026-10-18-01-10-57-1-ar-Default-string-165206"
    log_dir.mkdir()
    (log_dir / "launch.log").write_text((FIXTURES / "launch_activation_timeout.log").read_text())
    archive = Archive(tmp_path / "runs")
    kernel = (FIXTURES / "kernel_timeout.jsonl").read_text().splitlines()
    document = import_launch_dir(log_dir, profile, archive, scope="bench_left_arm", domain="77", hardware="ethercat", kernel_lines=kernel)
    assert document["result"] == "failed" and document["summary"]["failed_stage"] == "hardware_activation"
    run_dir = archive.root / document["id"]
    assert (run_dir / "report.html").exists() and "失败步骤" in (run_dir / "report.html").read_text()
    again = import_launch_dir(log_dir, profile, archive, scope="bench_left_arm", domain="77", hardware="ethercat", kernel_lines=kernel)
    assert again["id"] == document["id"]  # re-import updates the same run
    replayed, events = replay(run_dir, profile)
    assert replayed["summary"]["failed_stage"] == "hardware_activation"
    assert any(e.source == "kernel" for e in events)  # kernel events survive a replay
    row = read_json(archive.root / "index.json")["runs"][0]
    assert row["failed_stage"] == "hardware_activation" and row["anomalies"]["ecat.responding"] >= 1


def test_import_reads_scope_and_hardware_from_the_artifact_named_in_the_log(tmp_path, profile, artifact_dir):
    log_dir = tmp_path / "2026-10-18-21-03-00-1-ar-Default-string-273117"
    log_dir.mkdir()
    (log_dir / "launch.log").write_text(
        "1792328580.6 [INFO] [launch]: All log files can be found below /x\n"
        f"1792328581.0 [ERROR] [launch]: Caught exception in launch (see debug for traceback): Artifact is stale; "
        f"regenerate with: ros2 run rt_control_composer compose_artifact --output {artifact_dir} --real\n"
    )
    document = import_launch_dir(log_dir, profile, Archive(tmp_path / "runs"), domain="77", kernel_lines=[])
    assert document["scope"] == "bench_left_arm" and document["hardware"] == "ethercat"
    assert document["summary"]["failed_stage"] == "preflight"
    assert document["id"].endswith("-d77-bench_left_arm")


def test_compare_page_shows_stage_and_anomaly_differences(tmp_path, profile):
    archive = Archive(tmp_path / "runs")
    ids = []
    for name, kernel, pid in (("launch_normal.log", "kernel_normal_start.jsonl", 235346), ("launch_handoff_rescan.log", "kernel_rescan.jsonl", 89236)):
        log_dir = tmp_path / f"2026-10-18-00-00-00-1-ar-Default-string-{pid}"
        log_dir.mkdir()
        (log_dir / "launch.log").write_text((FIXTURES / name).read_text())
        document = import_launch_dir(log_dir, profile, archive, scope="bench_left_arm", domain="77",
                                     kernel_lines=(FIXTURES / kernel).read_text().splitlines())
        ids.append(document["id"])
    server = serve(archive, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(f"{base}/compare?a={ids[0]}&b={ids[1]}").read().decode()
        assert "两次运行对比" in page and "启动步骤" in page and "全部从站 OP" in page
        assert "ecat.not_synced" in page  # only in the rescan run
        index = urllib.request.urlopen(base + "/").read().decode()
        assert 'class="pick"' in index and "compareBtn" in index
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(f"{base}/compare?a={ids[0]}&b=../etc")
        assert error.value.code == 404
    finally:
        server.shutdown()


def test_web_is_read_only(tmp_path, profile):
    log_dir = tmp_path / "2026-10-17-19-04-36-1-ar-Default-string-96357"
    log_dir.mkdir()
    (log_dir / "launch.log").write_text((FIXTURES / "launch_preflight_reject.log").read_text())
    archive = Archive(tmp_path / "runs")
    document = import_launch_dir(log_dir, profile, archive, scope="bench_left_arm", kernel_lines=[])
    server = serve(archive, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        index = urllib.request.urlopen(base + "/").read().decode()
        assert document["id"] in index and "preflight" in index
        report = urllib.request.urlopen(f"{base}/{document['id']}/").read().decode()
        assert "启动前检查拒绝" in report
        assert urllib.request.urlopen(f"{base}/{document['id']}/run.json").status == 200
        for path in ("/../etc/passwd", f"/{document['id']}/../../x", f"/{document['id']}/secret.txt", "/nope/"):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + path)
            assert error.value.code == 404
        request = urllib.request.Request(base + "/", data=b"x", method="POST")
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 405
    finally:
        server.shutdown()
