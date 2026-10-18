"""Parsers against real bench output (launch.log, journal JSON, ethercat CLI, /proc)."""

from pathlib import Path

import pytest

from runtime_log import ethercat, kernlog
from runtime_log.launchlog import parse_line
from runtime_log.procfs import CpuSampler, ProcFS, format_cpu_list, parse_cpu_list, parse_stat
from runtime_log.sources import kernel_events

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_launch_line_shapes():
    launch = parse_line("1792306068.6345289 [INFO] [ros2_control_node-1]: process started with pid [235371]\n")
    assert launch.launch and launch.process == "ros2_control_node-1" and launch.ts == pytest.approx(1792306068.6345289)
    node = parse_line(
        "1792306073.4420040 [ros2_control_node-1] [INFO] [1792306073.441857505] [EthercatDriver]: "
        "All configured EtherCAT slaves are OP with a complete working counter"
    )
    assert not node.launch and node.logger == "EthercatDriver" and node.text.startswith("[EthercatDriver]: All")
    raw = parse_line("1792306073.4446051 [scope_hardware_start-2] RT_SCOPE_HARDWARE_ACTIVE_DISABLED")
    assert raw.level == "" and raw.message == "RT_SCOPE_HARDWARE_ACTIVE_DISABLED"
    colored = parse_line("\x1b[32m1792306073.4 [INFO] [launch]: Default logging verbosity is set to INFO\x1b[0m")
    assert colored.launch and colored.process == "launch"
    assert parse_line("garbage") is None


def test_kernel_messages_are_classified(fixture_text):
    events = kernel_events(fixture_text("kernel_rescan.jsonl").splitlines())
    rules = [e.rule for e in events]
    assert "responding" in rules and "rescan_now" in rules and "datagrams_unmatched" in rules
    not_synced = [e for e in events if e.rule == "not_synced"]
    assert not_synced and not_synced[0].subject.startswith("slave ") and not_synced[0].data["ms"] >= 5000
    al = [e for e in events if e.rule == "al_state_timeout"]
    assert al and al[0].level == "error"
    # timestamps are microsecond journal times and carry the kernel monotonic stamp
    assert all(e.mono is not None for e in events)
    assert not any("binding thread to cpu" in e.message for e in events)


def test_kernel_dropout_window_counts_responding_changes(fixture_text):
    events = kernel_events(fixture_text("kernel_dropout.jsonl").splitlines())
    counts = [e.data["count"] for e in events if e.rule == "responding"]
    assert 3 in counts and 9 in counts  # J1-J2 link drop (only 3 slaves answered) and recovery


def test_master_and_slaves_output(fixture_text):
    master = ethercat.parse_master(fixture_text("ethercat_master_op.txt"))
    assert master["phase"] == "Operation" and master["active"] is True and master["slaves"] == 9
    assert master["counters"]["lost_frames"] == 541 and master["counters"]["tx_errors"] == 732
    assert master["counters"]["tx_frames"] == 108821609
    slaves = ethercat.parse_slaves(fixture_text("ethercat_slaves_op.txt"))
    assert len(slaves) == 9 and slaves[1]["state"] == "OP" and slaves[8]["state"] == "PREOP"
    assert slaves[0]["name"].startswith("GR10-EC-6SW")


def test_poller_refuses_anything_but_master_and_slaves():
    calls = []
    poller = ethercat.EthercatPoller(runner=lambda argv: calls.append(argv) or "")
    poller.master_state()
    poller.slave_states()
    assert [argv[1] for argv in calls] == ["master", "slaves"]
    for forbidden in ("upload", "download", "reg_read", "reg_write", "sii_write", "states"):
        with pytest.raises(ValueError):
            poller._call(forbidden)


def test_counter_deltas_and_module_reload():
    tracker = ethercat.CounterTracker()
    assert tracker.update({"lost_frames": 10}) == {}
    assert tracker.update({"lost_frames": 13}) == {"lost_frames": 3}
    assert tracker.update({"lost_frames": 2}) == {"lost_frames": 2}  # reloaded master restarts at 0


def test_proc_stat_with_spaces_and_parentheses():
    stat = parse_stat("42 (weird (name) x) S 1 42 42 0 -1 4194560 0 0 0 0 7 3 0 0 -81 0 3 0 1234 100 25 "
                      + " ".join(["0"] * 14) + " 14 80 1 0 0 0")
    assert stat["comm"] == "weird (name) x" and stat["ppid"] == 1 and stat["utime"] == 7 and stat["stime"] == 3
    assert stat["processor"] == 14 and stat["rt_priority"] == 80 and stat["policy"] == "FIFO"


def test_proc_fixture_tree():
    proc = ProcFS(FIXTURES / "procfs/proc")
    pid = next(p for p in proc.pids())
    stat = proc.stat(pid)
    assert stat["comm"] == "ros2_control_no" and stat["num_threads"] == 36
    threads = proc.threads(pid)
    assert len(threads) == 36
    fifo = [proc.stat(pid, t) for t in threads if proc.stat(pid, t)["policy"] == "FIFO"]
    assert fifo and all(t["rt_priority"] > 0 for t in fifo)
    assert "controllers.yaml" in " ".join(proc.cmdline(pid))
    assert proc.boot_time() > 1_700_000_000


def test_cpu_list_round_trip_and_sampler():
    assert parse_cpu_list("0-7,9,11,13,15-27") == set(range(0, 8)) | {9, 11, 13} | set(range(15, 28))
    assert format_cpu_list({0, 2, 4, 6, 16, 17, 18}) == "0,2,4,6,16-18"
    sampler = CpuSampler(100)
    assert sampler.percent((1, 1), 10.0, 100) is None
    assert sampler.percent((1, 1), 11.0, 150) == pytest.approx(50.0)
