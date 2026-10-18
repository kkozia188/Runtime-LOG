"""M2 experiments: streaming to MCAP, per-joint tracking metrics, auto start/stop from JTC goals."""

import json
import math
import time
from pathlib import Path

import pytest

from runtime_log.blackbox import BlackBox, Settings
from runtime_log.experiment_report import render_experiment, render_experiment_compare
from runtime_log.model import Event
from test_blackbox import PyWriter

FIELDS = ["left_joint1/position", "left_joint1/target_position", "left_joint1/velocity", "left_joint1/torque_permille",
          "left_joint2/position", "left_joint2/target_position"]
BASE_NS = 1_800_000_000_000_000_000


def write_motion(writer, start_seq_time, lag=10, travel=0.1, ramp=500, settle=800):
    """Static 1000 cycles, a ramp of `ramp` cycles on joint 1 with the actual lagging `lag` cycles,
    a 0.0002 rad overshoot that decays, then `settle` static cycles."""
    targets = [0.0] * 1000 + [travel * (i + 1) / ramp for i in range(ramp)] + [travel] * settle
    for k, target in enumerate(targets):
        actual = targets[max(0, k - lag)]
        after = k - (1000 + ramp + lag)
        if 0 <= after < 50:
            actual = travel + 0.0002 * (1 - after / 50)  # overshoot decaying to the target within 50 ms
        velocity = (targets[k] - targets[k - 1]) * 1000 if k else 0.0
        writer.write([actual, target, velocity, 3.0, 0.5, 0.5], time_ns=BASE_NS + (k + 1) * 1_000_000)


def test_experiment_metrics_and_mcap(tmp_path):
    mcap = pytest.importorskip("mcap")  # noqa: F841
    writer = PyWriter(tmp_path / "seg", FIELDS, slots=4000)
    events = []
    box = BlackBox(writer.path, tmp_path / "run" / "blackbox", Settings(stall_s=3600), on_event=events.append)
    assert box.tracking
    write_motion(writer, 0)
    box._step(final=False)
    start = (BASE_NS + 900 * 1_000_000) / 1e9
    stop = (BASE_NS + 2200 * 1_000_000) / 1e9
    box.start_experiment("goal-1", "auto:jtc_goal", start, {"intent": "去位姿 home"})
    box.stop_experiment(stop)
    box._step(final=True)
    saved = [e for e in events if e["kind"] == "experiment_saved"]
    assert len(saved) == 1
    directory = tmp_path / "run" / "experiments" / saved[0]["dir"]
    meta = json.loads((directory / "meta.json").read_text())
    assert meta["cycles"] == 1301 and meta["missing_cycles"] == 0 and meta["meta"]["intent"] == "去位姿 home"
    j1, j2 = meta["joints"]["left_joint1"], meta["joints"]["left_joint2"]
    assert j1["moved"] and not j2["moved"]
    assert j1["travel_deg"] == pytest.approx(math.degrees(0.1), abs=1e-3)
    # 10-cycle lag at 0.2 rad/s -> 0.002 rad = 0.1146 deg
    assert j1["max_error_deg"] == pytest.approx(math.degrees(0.002), rel=0.02)
    assert j1["overshoot_deg"] == pytest.approx(math.degrees(0.0002), rel=0.05)
    assert 0.0 < j1["settle_time_s"] < 0.1 and j1["final_error_deg"] == pytest.approx(0.0, abs=1e-6)
    assert j1["peak_torque_permille"] == 3.0 and j1["peak_velocity_deg_s"] == pytest.approx(math.degrees(0.2), rel=0.01)
    assert meta["summary"]["moving_joints"] == 1 and meta["summary"]["unsettled"] == []
    from mcap.reader import make_reader

    with (directory / "data.mcap").open("rb") as stream:
        count = sum(1 for _ in make_reader(stream).iter_messages())
    assert count == 1301
    plot = json.loads((directory / "plot.json").read_text())
    assert max(abs(v) for v in plot["left_joint1"]["error_deg"]) == pytest.approx(math.degrees(0.002), rel=0.02)
    page = render_experiment("run-1", meta, plot)
    assert "最大跟随误差" in page and "left_joint1" in page and "<svg" in page
    assert "B − A" in render_experiment_compare(("a", meta), ("b", meta))
    assert box.describe()["experiments"][0]["name"] == "goal-1"


def test_unsettled_joint_is_reported(tmp_path):
    pytest.importorskip("mcap")
    writer = PyWriter(tmp_path / "seg", FIELDS, slots=4000)
    events = []
    box = BlackBox(writer.path, tmp_path / "run" / "blackbox", Settings(stall_s=3600), on_event=events.append)
    write_motion(writer, 0, lag=400, settle=100)  # still far behind when the experiment stops
    box._step(final=False)
    box.start_experiment("slow", "manual", (BASE_NS + 900 * 1_000_000) / 1e9)
    box._step(final=True)  # writer "ends": experiment closes at the last cycle
    meta = json.loads((tmp_path / "run" / "experiments" / events[-1]["dir"] / "meta.json").read_text())
    assert meta["joints"]["left_joint1"]["settle_time_s"] is None
    assert meta["summary"]["unsettled"] == ["left_joint1"]


def test_segments_without_targets_never_record_experiments(tmp_path):
    writer = PyWriter(tmp_path / "seg", ["phase", "owner"])
    box = BlackBox(writer.path, tmp_path / "bb", Settings(stall_s=3600))
    assert not box.tracking and box.start_experiment("x", "manual", 0.0) is False


def test_recorder_starts_one_experiment_per_live_goal_and_ignores_replays(tmp_path, profile):
    from runtime_log.recorder import RunRecorder

    class Box:
        tracking = True

        def __init__(self):
            self.calls = []

        def start_experiment(self, *args):
            self.calls.append(("start",) + args)

        def stop_experiment(self, ts):
            self.calls.append(("stop", ts))

    recorder = RunRecorder.__new__(RunRecorder)  # only the hook state is needed
    recorder.clock = time.time
    recorder.goal_count = 0
    recorder.last_operator = None
    box = Box()
    recorder._active_boxes = lambda: [box]
    now = time.time()
    recorder._experiment_hooks(Event(now - 60, "control", "launch", rule="goal_accepted"))  # replayed: ignored
    recorder._experiment_hooks(Event(now - 1.2, "operator", "console", message="home；档位 low", label="去位姿"))
    recorder._experiment_hooks(Event(now - 1.0, "control", "launch", rule="goal_accepted", message="Accepted new action goal"))
    recorder._experiment_hooks(Event(now - 0.5, "control", "launch", rule="goal_reached"))
    assert [c[0] for c in box.calls] == ["start", "stop"]
    start = box.calls[0]
    assert start[1] == "goal-1" and start[2] == "auto:jtc_goal" and start[3] == pytest.approx(now - 1.2)
    assert start[4]["intent"] == "去位姿 home"
    assert box.calls[1][1] == pytest.approx(now + 0.5)
