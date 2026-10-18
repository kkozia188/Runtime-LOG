"""Run results for real bench runs: normal stop, activation timeout, preflight rejection, rescan."""

from runtime_log.model import CONTROL, FAILURE, STAGE
from runtime_log.sources import kernel_events, launch_events
from runtime_log.timeline import summarize


def build(fixture_text, profile, launch, kernel=None):
    events = launch_events(fixture_text(launch).splitlines(), profile)
    if kernel:
        events += kernel_events(fixture_text(kernel).splitlines())
    return events, summarize(events, profile.ready_stages)


def test_normal_run_is_ok_and_the_shutdown_exit_is_not_a_failure(fixture_text, profile):
    events, summary = build(fixture_text, profile, "launch_normal.log", "kernel_normal_start.jsonl")
    assert summary["result"] == "ok" and summary["failure"] is None
    assert summary["anomalies"].get("process.nonzero_exit_during_shutdown") == 1  # scope_capabilities exit 1
    assert "process.died_before_shutdown" not in summary["anomalies"]
    stages = [e.rule for e in events if e.kind == STAGE]
    for expected in ("ecat_master_active", "ecat_all_op", "ecat_handoff", "hardware_active_disabled", "controller_activated"):
        assert expected in stages
    assert 4.0 < summary["ready_s"] < 7.0  # launch -> last controller active (~6.3 s)
    controls = [e.rule for e in events if e.kind == CONTROL]
    assert controls.count("rt_enable") == 5 and controls.count("goal_reached") == 23
    names = [p["base"] for p in summary["processes"]]
    assert names[0] == "ros2_control_node" and "scope_capabilities" in names


def test_activation_timeout_names_the_failed_step_and_the_last_good_stage(fixture_text, profile):
    events, summary = build(fixture_text, profile, "launch_activation_timeout.log", "kernel_timeout.jsonl")
    assert summary["result"] == "failed" and summary["failed_stage"] == "hardware_activation"
    assert summary["failure"]["rule"] == "activation_timeout"
    assert summary["last_stage"]["rule"] == "ecat_master_active"
    assert 69.0 < summary["failure"]["t_rel_s"] < 72.0  # 70 s startup timeout
    # the drives had dropped off the bus: the kernel shows only 2 responders
    assert summary["anomalies"]["ecat.responding"] >= 1
    assert not [e for e in events if e.kind == STAGE and e.rule == "ecat_all_op"]


def test_preflight_rejection(fixture_text, profile):
    _, summary = build(fixture_text, profile, "launch_preflight_reject.log")
    assert summary["result"] == "failed" and summary["failed_stage"] == "preflight"
    assert summary["processes"] == []


def test_handoff_rescan_run_is_ok_but_counts_bus_anomalies(fixture_text, profile):
    _, summary = build(fixture_text, profile, "launch_handoff_rescan.log", "kernel_rescan.jsonl")
    assert summary["result"] == "ok"
    anomalies = summary["anomalies"]
    assert anomalies["ecat.not_synced"] == 3
    assert anomalies["ecat.datagrams_unmatched"] >= 3559
    assert anomalies["ecat.al_state_timeout"] == 1
    assert summary["anomaly_total"] > 0


def test_failure_rules_win_over_stage_rules(profile):
    from runtime_log.launchlog import parse_line

    line = parse_line(
        "1.0 [ros2_control_node-1] [ERROR] [1.0] [EthercatDriver]: Timed out waiting for all configured slaves OP and a complete working counter"
    )
    events = profile.classify(line)
    assert [e.kind for e in events] == [FAILURE]
