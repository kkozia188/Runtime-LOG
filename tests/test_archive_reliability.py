"""Independent archives and continued sampling after a failed blackbox window.

Adapted from robot_driver PR #189 (55f5742), with failure-stage and thread-liveness checks.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event as ThreadEvent

import pytest
from mcap.reader import make_reader

from runtime_log import blackbox
from test_blackbox import FIELDS, PyWriter, make_box, run_cycles
from test_recording import artifact_dir as artifact_dir  # Re-export shared pytest fixture.


def messages(path):
    with path.open('rb') as stream:
        return [json.loads(message.data) for _, _, message in make_reader(stream).iter_messages()]


def test_simultaneous_segments_keep_independent_valid_windows(tmp_path, monkeypatch):
    boxes = []
    for i in range(2):
        writer = PyWriter(tmp_path / f'segment-{i}', FIELDS + [f'identity_{i}'])
        box, _ = make_box(tmp_path, writer.path, post_s=0)
        for seq in range(10):
            writer.write([seq, 0x1637, 8, 0, 0, i])
        box._step(final=False)
        box.trigger('cycle_overrun')
        boxes.append(box)
    barrier = Barrier(2)
    original = blackbox.write_mcap

    def overlap(*args):
        barrier.wait(timeout=5)
        return original(*args)

    monkeypatch.setattr(blackbox, 'write_mcap', overlap)
    try:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda box: box._step(final=False), boxes))
        assert all(len(box.saved) == 1 for box in boxes)
        assert len({box.saved[0] for box in boxes}) == 2
        for i, box in enumerate(boxes):
            directory = box.out_dir / box.saved[0]
            assert json.loads((directory / 'trigger.json').read_text())['segment'] == f'segment-{i}'
            rows = messages(directory / 'window.mcap')
            assert len(rows) == 10
            assert all(row[f'identity_{i}'] == i for row in rows)
    finally:
        for box in boxes:
            box.reader.close()


@pytest.mark.parametrize('stage', ['mcap', 'kernel', 'metadata'])
def test_failed_window_is_not_published_and_sampling_continues(tmp_path, monkeypatch, stage):
    writer = PyWriter(tmp_path / 'segment', FIELDS)
    box, events = make_box(tmp_path, writer.path, post_s=0)
    try:
        run_cycles(writer, 10)
        box._step(final=False)
        box.trigger('manual')
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError('injected archive failure')
            if stage == 'mcap':
                patch.setattr(blackbox, 'write_mcap', fail)
            elif stage == 'kernel':
                patch.setattr(box, 'kernel_lines', fail)
            else:
                patch.setattr(blackbox, 'write_json', fail)
            box._step(final=False)
            assert not box.saved and box.pending is None
            assert events[-1]['kind'] == 'blackbox_error'
            assert 'injected archive failure' in events[-1]['detail']
            assert not list(box.out_dir.glob('*/trigger.json'))
            box._step(final=False)
            assert len([e for e in events if e['kind'] == 'blackbox_error']) == 1
        run_cycles(writer, 10)
        box.trigger('manual')
        box._step(final=False)
        assert box.describe()['cycle_stats']['cycles'] == 20
        assert len(box.describe()['archive_errors']) == 1
        assert len(box.saved) == 1
        assert len(messages(box.out_dir / box.saved[0] / 'window.mcap')) == 20
    finally:
        box.reader.close()


def test_real_reader_thread_survives_archive_failure(tmp_path, monkeypatch):
    writer = PyWriter(tmp_path / 'segment', FIELDS)
    box, _ = make_box(tmp_path, writer.path, post_s=0, interval_s=0.005)
    failed, saved = ThreadEvent(), ThreadEvent()
    box.on_event = lambda event: (failed.set() if event['kind'] == 'blackbox_error' else
                                 saved.set() if event['kind'] == 'blackbox_saved' else None)
    original = blackbox.write_mcap
    calls = 0

    def fail_first(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError('disk unavailable')
        return original(*args)

    monkeypatch.setattr(blackbox, 'write_mcap', fail_first)
    run_cycles(writer, 10)
    box.trigger('manual')
    box.start()
    try:
        assert failed.wait(3), 'archive error not reported'
        assert box.is_alive()
        run_cycles(writer, 10)
        box.trigger('manual')
        assert saved.wait(3), 'reader did not recover'
        assert box.is_alive() and box.describe()['cycle_stats']['cycles'] == 20
    finally:
        box.stop()
        box.join(timeout=3)
        assert not box.is_alive()
        box.reader.close()


@pytest.mark.parametrize('continuous', [False, True])
def test_two_segments_never_overwrite_experiments(tmp_path, continuous):
    boxes = []
    try:
        for i in range(2):
            fields = [f'joint_{i}/position', f'joint_{i}/target_position']
            writer = PyWriter(tmp_path / f'segment-{i}', fields)
            # Both writers deliberately share the same writer name and timestamps.
            box, _ = make_box(tmp_path, writer.path, continuous_experiments=continuous,
                              experiment_min_free_gb=0)
            boxes.append(box)
            for _ in range(10):
                writer.write([i, i])
            box._step(final=False)
            if not continuous:
                box.start_experiment('goal-1', 'manual', 1_800_000_000.0)
                box._step(final=False)
        assert len({box.experiment.directory for box in boxes}) == 2
        for i, box in enumerate(boxes):
            directory = box.experiment.directory
            if not continuous:
                assert directory.name.endswith('-goal-1')
            box._step(final=True)
            rows = messages(directory / 'data.mcap')
            assert len(rows) == 10
            assert all(row[f'joint_{i}/position'] == i for row in rows)
    finally:
        for box in boxes:
            box.reader.close()


def test_same_segment_same_trigger_never_reuses_archive_directory(tmp_path):
    writer = PyWriter(tmp_path / 'segment', FIELDS)
    box, _ = make_box(tmp_path, writer.path, post_s=0)
    try:
        run_cycles(writer, 10)
        for _ in range(2):
            box.trigger('manual')
            box._step(final=False)
        assert len(set(box.saved)) == 2
        assert all(len(messages(box.out_dir / name / 'window.mcap')) == 10 for name in box.saved)
    finally:
        box.reader.close()


def test_archive_error_is_visible_in_run_timeline_and_summary(tmp_path, profile, artifact_dir):
    from runtime_log.archive import Archive
    from runtime_log.recorder import RunRecorder
    from runtime_log.timeline import summarize
    from test_recording import LAUNCH_PID, FIXTURES, FakeProc

    log_dir = tmp_path / f'x-{LAUNCH_PID}'
    log_dir.mkdir()
    (log_dir / 'launch.log').write_text((FIXTURES / 'launch_normal.log').read_text())
    recorder = RunRecorder(LAUNCH_PID, log_dir, profile, Archive(tmp_path / 'runs'), FakeProc(artifact_dir), None)
    recorder.blackbox_events.put({'kind': 'blackbox_error', 'ts': recorder.start + 1,
                                 'segment': 'segment', 'reasons': ['manual'], 'detail': 'disk full'})
    try:
        recorder.drain_blackbox_events()
        event = recorder.events[-1]
        assert event.rule == 'blackbox_error' and event.level == 'error'
        assert event.message == 'disk full'
        assert summarize(recorder.events)['anomalies']['blackbox.archive_errors'] == 1
    finally:
        recorder.finish(recorder.start + 2)
