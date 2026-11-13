"""Controller log errors use the existing blackbox, without replaying historical errors."""
from types import SimpleNamespace
from unittest.mock import Mock
import json

from mcap.reader import make_reader
import pytest

from runtime_log.blackbox import BlackBox, Settings
from runtime_log.launchlog import parse_line
from runtime_log.recorder import LogTail, RunRecorder
from test_blackbox import FIELDS, PyWriter


def recorder_for(profile, now, boxes):
    recorder = RunRecorder.__new__(RunRecorder)
    recorder.profile = profile
    recorder.capture_started = now - 1
    recorder.clock = lambda: now
    recorder.last_controller_error_capture = float('-inf')
    recorder.artifact = {'controllers': {'dual_arm_jtc': 'joint_trajectory_controller/JointTrajectoryController'}}
    recorder.blackboxes = {str(i): box for i, box in enumerate(boxes)}
    recorder.control_pid = None
    return recorder


def line_at(ts, level='ERROR', logger='dual_arm_jtc'):
    return parse_line(f'{ts:.6f} [ros2_control_node-4] [{level}] [{ts:.6f}] [{logger}]: FJT_REJECT reason=acceleration')


@pytest.mark.parametrize('level,logger,expected', [
    ('ERROR', 'dual_arm_jtc', True), ('FATAL', 'dual_arm_jtc', True),
    ('ERROR', 'controller_manager', True), ('ERROR', 'robot.dual_arm_jtc', True),
    ('WARN', 'dual_arm_jtc', False), ('ERROR', 'unrelated_node', False)])
def test_only_fresh_configured_controller_errors_trigger(profile, level, logger, expected):
    box = Mock()
    recorder = recorder_for(profile, 100, [box])
    recorder._capture_controller_error(line_at(100, level, logger))
    assert box.trigger.call_count == int(expected)
    if expected:
        assert box.trigger.call_args.args[0] == 'controller_error'
        assert box.trigger.call_args.args[1] == 100


def test_restart_replay_disabled_setting_and_error_bursts(profile):
    box = Mock()
    recorder = recorder_for(profile, 100, [box])
    recorder._capture_controller_error(line_at(98))
    assert not box.trigger.called  # Before capture started: no historical backfill.
    recorder.capture_started = 0
    recorder._capture_controller_error(line_at(70))
    recorder._capture_controller_error(line_at(101))
    assert not box.trigger.called  # Outside retained-history budget or future timestamp.
    recorder._capture_controller_error(line_at(99))
    recorder._capture_controller_error(line_at(100))
    assert box.trigger.call_count == 1
    recorder.clock = lambda: 102
    recorder._capture_controller_error(line_at(102))
    assert box.trigger.call_count == 2
    recorder.profile = SimpleNamespace(blackbox={'capture_controller_errors': False})
    recorder.clock = lambda: 104
    recorder._capture_controller_error(line_at(104))
    assert box.trigger.call_count == 2


def test_rejected_goal_saves_ten_seconds_before_two_after_without_experiment(tmp_path, profile):
    writer = PyWriter(tmp_path/'segment', FIELDS, slots=16000)
    events = []
    box = BlackBox(writer.path, tmp_path/'blackbox', Settings(post_s=2, pre_s=10, stall_s=3600),
                   on_event=events.append, explain=lambda *_: '/rt/disable')
    origin = 1800000000
    trigger_time = origin + 10.5
    recorder = recorder_for(profile, trigger_time, [box])
    output = []
    recorder._emit = output.append
    log = tmp_path/'launch.log'
    log.write_text(f'{trigger_time:.6f} [ros2_control_node-4] [ERROR] [{trigger_time:.6f}] [dual_arm_jtc]: FJT_REJECT reason=acceleration\n')
    recorder.tail = LogTail(log)
    try:
        for i in range(1, 10501):
            writer.write([i/1000, 0x1637, 8, 0, 0])
        box._step(False)
        recorder.poll_log()
        assert len(output) == 1 and output[0].level == 'error'
        assert not box.experiments  # A rejected goal never started a JTC experiment.
        box._step(False)
        assert box.pending is not None and not box.saved
        for i in range(10501, 12501):
            writer.write([i/1000, 0x1637, 8, 0, 0])
        box._step(False)
        assert len(box.saved) == 1
        directory = box.out_dir/box.saved[0]
        meta = json.loads((directory/'trigger.json').read_text())
        assert meta['reasons'] == ['controller_error']
        assert meta['window']['cycles'] == 12001
        assert meta['window']['start'] == pytest.approx(trigger_time-10)
        assert meta['window']['end'] == pytest.approx(trigger_time+2)
        assert 'FJT_REJECT' in meta['triggers'][0]['detail']
        with (directory/'window.mcap').open('rb') as stream:
            assert sum(1 for _ in make_reader(stream).iter_messages()) == 12001
    finally:
        box.reader.close()
