"""Continuous recording is a non-RT reader policy; bounded files and explicit low-disk pauses."""
import json
from collections import namedtuple

import pytest

from runtime_log.blackbox import BlackBox, Settings
from runtime_log.experiment_report import experiments_section
from test_blackbox import PyWriter

BASE = 1_800_000_000_000_000_000
FIELDS = ['wheel1_joint/position', 'wheel1_joint/velocity', 'wheel1_joint/sent_velocity',
          'wheel1_joint/status_word', 'wheel1_joint/mode_of_operation_display', 'wheel1_joint/feedback_age_ms']


def make(tmp_path, **settings):
    writer = PyWriter(tmp_path / 'segment', FIELDS, slots=4000)
    box = BlackBox(writer.path, tmp_path / 'run/blackbox', Settings(
        stall_s=3600, experiment_max_s=.1, continuous_experiments=True, experiment_min_free_gb=0, **settings))
    return writer, box


def samples(writer, n):
    for _ in range(n):
        seq = writer.next
        writer.write([0, 0, 0, 0x40, 9, 1], time_ns=BASE + seq * 1_000_000)


def saved(box):
    return sorted((json.loads(p.read_text()) for p in box.experiments_dir.glob('*/meta.json')), key=lambda m: m['first_seq'])


def test_continuous_rotates_without_cycle_gaps_even_on_final_batch(tmp_path):
    writer, box = make(tmp_path)
    samples(writer, 500)
    box._step(final=False)
    samples(writer, 120)
    writer.state(2)
    box._step(final=True)
    docs = saved(box)
    assert len(docs) >= 6
    seqs = [n for m in docs for n in range(m['first_seq'], m['last_seq'] + 1)]
    assert seqs == list(range(1, 621))
    assert all(m['source'] == 'auto:continuous' and m['missing_cycles'] == 0 for m in docs)
    assert all(m['summary']['no_valid_tracking_data'] for m in docs)  # disabled isn't a performance pass
    box.reader.close()


def test_service_restart_skips_already_archived_ring_samples(tmp_path):
    writer, box = make(tmp_path)
    samples(writer, 150)
    box._step(final=False)
    box._step(final=True)
    box.reader.close()
    restarted = BlackBox(writer.path, box.out_dir, box.settings)
    samples(writer, 150)
    restarted._step(final=False)
    restarted._step(final=True)
    docs = saved(restarted)
    seqs = [n for m in docs for n in range(m['first_seq'], m['last_seq'] + 1)]
    assert seqs == list(range(1, 301))
    restarted.reader.close()


def test_manual_stop_pauses_continuous_until_new_recorder(tmp_path):
    writer, box = make(tmp_path)
    samples(writer, 50)
    box._step(final=False)
    box.stop_experiment((BASE + 50 * 1_000_000) / 1e9)
    box._step(final=False)
    assert box.continuous_paused and box.experiment is None
    samples(writer, 50)
    box._step(final=False)
    assert box.experiment is None and saved(box)[0]['last_seq'] == 50
    box.reader.close()


def test_low_space_pauses_and_closes_current_segment_but_keeps_reading(tmp_path, monkeypatch):
    writer, box = make(tmp_path)
    usage = namedtuple('Usage', 'total used free')
    monkeypatch.setattr('runtime_log.blackbox.shutil.disk_usage', lambda _: usage(2**40, 0, 2**40))
    samples(writer, 50)
    box._step(final=False)
    assert box.experiment.cycles == 50
    monkeypatch.setattr('runtime_log.blackbox.shutil.disk_usage', lambda _: usage(2**40, 2**40, 0))
    box._next_disk_check = 0
    samples(writer, 20)
    box._step(final=False)
    assert box.experiment is None and not box._continuous_disk_ok
    assert len(box.history) == 70
    assert saved(box)[0]['meta']['truncated'] == 'disk space guard'
    monkeypatch.setattr('runtime_log.blackbox.shutil.disk_usage', lambda _: usage(2**40, 0, 2**40))
    box._next_disk_check = 0
    samples(writer, 10)
    box._step(final=False)
    assert box.experiment.first_seq == 71
    box._step(final=True)
    box.reader.close()


def test_continuous_disabled_by_default_and_ui_marks_active(tmp_path):
    writer = PyWriter(tmp_path / 'segment', FIELDS)
    box = BlackBox(writer.path, tmp_path / 'bb', Settings(stall_s=3600))
    samples(writer, 100)
    box._step(final=False)
    assert box.experiment is None
    box.settings.continuous_experiments = True
    box.experiments_dir.mkdir(exist_ok=True)
    samples(writer, 10)
    box._step(final=False)
    page = experiments_section({'blackbox': [box.describe()]}, None)
    assert '正在记录' in page and '分段完成' in page
    box._step(final=True)
    box.reader.close()


@pytest.mark.parametrize('key,value', [('experiment_max_s', 0), ('experiment_max_s', float('nan')),
                                      ('experiment_min_free_gb', -1)])
def test_invalid_continuous_settings_rejected(tmp_path, key, value):
    writer = PyWriter(tmp_path / 'segment', FIELDS)
    with pytest.raises(ValueError):
        BlackBox(writer.path, tmp_path / 'bb', Settings(**{key: value}))
