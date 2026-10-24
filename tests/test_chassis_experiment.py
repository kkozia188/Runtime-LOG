"""Non-RT chassis experiment tests: raw capture, CSV-only metrics and waveform validity."""
import json
import math
import struct

import pytest

from runtime_log.blackbox import BlackBox, Settings
from runtime_log.experiment import Experiment, has_tracking_fields
from runtime_log.experiment_report import render_experiment, render_experiment_compare, experiments_section
from runtime_log.velocity_experiment import VelocityMetrics, sample_error
from runtime_log.waveform import waveform
from test_blackbox import PyWriter

BASE_NS = 1_800_000_000_000_000_000
JOINT = 'wheel1_joint'


def healthy(**changes):
    row = {f'{JOINT}/{k}': v for k, v in {
        'position': 0.0, 'velocity': 0.8, 'sent_velocity': 1.0, 'mode_of_operation_display': 9,
        'status_word': 0x27, 'feedback_age_ms': 1.0, 'mode_request_ack': 1,
        'command_fresh': 1, 'mode_request_error': 0}.items()}
    row.update({f'{JOINT}/{k}': v for k, v in changes.items()})
    return row


def test_constant_speed_is_motion_and_signed_error_is_preserved():
    metric = VelocityMetrics(JOINT)
    for i in range(100):
        metric.add(healthy(), i / 1000)
    r = metric.result()
    assert r['samples'] == r['active_samples'] == 100 and r['moved']
    assert r['max_error_rad_s'] == pytest.approx(0.2)
    assert r['rms_error_rad_s'] == pytest.approx(0.2)
    assert r['mean_error_rad_s'] == pytest.approx(0.2)
    metric.add(healthy(sent_velocity=-1, velocity=-1.5), 1)
    assert metric.result()['max_error_rad_s'] == 0.5


@pytest.mark.parametrize('change,reason', [
    ({'mode_of_operation_display': 8}, 'not_csv'), ({'status_word': 0x40}, 'not_enabled'),
    ({'status_word': 0x1208}, 'not_enabled'), ({'feedback_age_ms': 21}, 'feedback_stale'),
    ({'feedback_age_ms': -1}, 'feedback_stale'), ({'command_fresh': 0}, 'command_fresh'),
    ({'mode_request_ack': 0}, 'mode_request_ack'), ({'mode_request_error': 1}, 'mode_request_error'),
    ({'velocity': math.nan}, 'nonfinite'), ({'sent_velocity': math.inf}, 'nonfinite')])
def test_invalid_samples_never_look_like_good_tracking(change, reason):
    row = healthy(**change)
    assert sample_error(row, JOINT) == (None, reason)
    m = VelocityMetrics(JOINT)
    m.add(row, 0)
    r = m.result()
    assert r['samples'] == 0 and r['max_error_rad_s'] is None and r['rms_error_rad_s'] is None
    assert r['excluded'] == {reason: 1}
    json.dumps(r, allow_nan=False)


def write_experiment(tmp_path):
    row = healthy()
    row.update({'steer1_joint/position': 0.5, 'steer1_joint/velocity': 0,
                'steer1_joint/sent_velocity': 0, 'steer1_joint/mode_of_operation_display': 8,
                'steer1_joint/status_word': 0x27, 'steer1_joint/feedback_age_ms': 1,
                'steering_encoder_1/position': 0.49, 'steering_encoder_1/feedback_age_ms': 4,
                'steering_encoder_1/ready': 1})
    fields = list(row)
    assert has_tracking_fields(fields)
    exp = Experiment(tmp_path, 'chassis-speed', 'manual', fields, 'runtime_log_recorder', BASE_NS / 1e9)
    for i in range(3000):
        values = dict(row)
        if i < 200 or i >= 2500:
            values[f'{JOINT}/status_word'] = 0x40
        if 1000 <= i < 1100:
            values[f'{JOINT}/feedback_age_ms'] = 30
        seq = i + 1 + (1 if i >= 1500 else 0)
        slot = struct.pack('<Qqq', seq, BASE_NS + i * 1_000_000, 1_000_000)
        slot += struct.pack(f'<{len(fields)}d', *[values[k] for k in fields])
        exp.add(seq, slot)
    exp.stop_ts = BASE_NS / 1e9 + 2.999
    return exp.finish()


def test_chassis_archive_metrics_and_website(tmp_path):
    meta = write_experiment(tmp_path)
    m = meta['velocity_joints'][JOINT]
    assert m['samples'] == 2200 and m['active_samples'] == 2200
    assert m['excluded'] == {'not_enabled': 700, 'feedback_stale': 100}
    assert m['rms_error_rad_s'] == pytest.approx(0.2)
    assert meta['missing_cycles'] == 1
    assert meta['velocity_joints']['steer1_joint']['samples'] == 0
    assert meta['summary']['tracking_kind'] == 'velocity'
    assert meta['summary']['max_error_rad_s'] == pytest.approx(0.2)
    assert 'steering_encoder_1' in meta['waveform_channels']
    page = render_experiment('run', meta, {})
    assert '行走轮速度性能' in page and '0.2' in page and 'rad/s' in page
    assert 'steering_encoder_1' in page
    assert '到位：终点误差' not in page
    comparison = render_experiment_compare(('a', meta), ('b', meta))
    assert '最大速度误差 rad/s' in comparison and '未到位' not in comparison
    body = experiments_section({'id': 'run', 'blackbox': [{'experiments': [{'dir': 'x', 'name': 'x', 'summary': meta['summary']}]}]}, None)
    assert '0.2 rad/s' in body and '未到位' not in body.replace('<th>未到位</th>', '')


def test_chassis_waveforms_show_rad_s_and_gap_invalid_speed_error(tmp_path):
    write_experiment(tmp_path)
    data = waveform(tmp_path / 'data.mcap', JOINT, 0, 2.999, width=2000)
    signals = {s['key']: s for s in data['series']}
    assert signals['velocity']['unit'] == 'rad/s'
    assert signals['sent_velocity']['unit'] == 'rad/s'
    error = dict(signals['velocity_error']['points'])
    assert error[0] is None
    assert any(v == pytest.approx(0.2) for v in error.values() if v is not None)
    assert error[1.0] is None
    assert data['missing'] == 1
    assert 'error' not in signals  # no position target; don't invent steering/position tracking
    steering = waveform(tmp_path / 'data.mcap', 'steer1_joint')
    by_key = {s['key']: s for s in steering['series']}
    assert all(v is None for _, v in by_key['sent_velocity']['points'])
    assert all(v is None for _, v in by_key['velocity_error']['points'])
    encoder = waveform(tmp_path / 'data.mcap', 'steering_encoder_1')
    assert {s['key'] for s in encoder['series']} >= {'position', 'feedback_age_ms', 'ready'}
    json.dumps(data, allow_nan=False)


def test_chassis_manual_recording_works_without_position_targets(tmp_path):
    row = healthy(status_word=0x40)
    writer = PyWriter(tmp_path / 'segment', list(row), slots=3000)
    events = []
    box = BlackBox(writer.path, tmp_path / 'run/blackbox', Settings(stall_s=3600), on_event=events.append)
    assert box.tracking
    for i in range(1000):
        writer.write(list(row.values()), time_ns=BASE_NS + i * 1_000_000)
    box._step(final=False)
    assert box.start_experiment('disabled-check', 'manual', BASE_NS / 1e9 + 0.2)
    box.stop_experiment(BASE_NS / 1e9 + 0.8)
    box._step(final=True)
    saved = [e for e in events if e['kind'] == 'experiment_saved']
    assert len(saved) == 1
    meta = json.loads((tmp_path / 'run/experiments' / saved[0]['dir'] / 'meta.json').read_text())
    assert meta['cycles'] == 601
    assert meta['summary']['no_valid_tracking_data']
    assert meta['velocity_joints'][JOINT]['samples'] == 0
    box.reader.close()
