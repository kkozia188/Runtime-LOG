"""Regression coverage for the read-only diagnostic workbench and time alignment."""
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from mcap.writer import Writer

from runtime_log.analysis import analysis_data, evidence_of, render_analysis
from runtime_log.archive import Archive, write_json
from runtime_log.index_view import render_home
from runtime_log.model import Event
from runtime_log.web import serve
from runtime_log.waveform import waveform


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path / 'runs'
    run = root / 'run'
    box = run / 'blackbox' / 'window'
    box.mkdir(parents=True)
    origin = 1800000000
    write_json(run / 'run.json', {'id': 'run', 'start': origin, 'result': 'ok', 'summary': {'duration_s': 30}})
    write_json(box / 'trigger.json', {'fields': ['wheel/position'], 'window': {'start': origin + 10, 'end': origin + 10.01}})
    with (box / 'window.mcap').open('wb') as stream:
        writer = Writer(stream); writer.start(); channel = writer.register_channel('/cycles', 'json', 0)
        for i in range(11):
            ns = (origin + 10) * 10**9 + i * 10**6
            writer.add_message(channel, log_time=ns, publish_time=ns,
                               data=json.dumps({'seq': i, 'period_us': 1000, 'wheel/position': i / 1000}).encode())
        writer.finish()
    return root, run, box, origin


def test_waveform_run_relative_alignment_uses_actual_mcap(evidence):
    _, _, box, origin = evidence
    data = waveform(box / 'window.mcap', 'wheel', 10.002, 10.006, time_origin=origin)
    assert data['samples'] == 5
    assert data['available_start'] == 10
    assert data['start'] == 10.002
    assert data['origin_s'] == origin
    assert data['series'][0]['points'][0][0] == pytest.approx(10.002)
    clipped = waveform(box / 'window.mcap', 'wheel', 2, 20, time_origin=origin)
    assert clipped['start'] == 10 and clipped['end'] == pytest.approx(10.01)
    assert clipped['samples'] == 11
    assert waveform(box / 'window.mcap', 'wheel', 0, 1, time_origin=origin)['samples'] == 0
    assert waveform(box / 'window.mcap', 'wheel', 20, 21, time_origin=origin)['samples'] == 0


@pytest.mark.parametrize('origin', [float('nan'), float('inf'), -1])
def test_reject_invalid_time_origin(evidence, origin):
    with pytest.raises(ValueError, match='时间基准'):
        waveform(evidence[2] / 'window.mcap', 'wheel', time_origin=origin)


def test_evidence_metadata_does_not_require_reading_all_samples(evidence):
    _, run, box, origin = evidence
    rows = evidence_of(run, origin)
    assert len(rows) == 1 and rows[0]['start'] == 10
    assert rows[0]['endpoint'] == 'blackbox/window/waveform.json'
    assert rows[0]['channels'] == ['wheel']
    (box / 'window.mcap').unlink()
    assert evidence_of(run, origin) == []
    assert evidence_of(None, origin) == []


def test_expected_transition_is_not_a_fault_and_logs_are_safely_embedded():
    malicious = '</script><img src=x onerror=alert(1)>'
    events = [Event(101, 'state', 'launch', message=malicious, level='warn', rule='blackbox_expected'),
              Event(102, 'ecat', 'kernel', message='WKC error', level='error'),
              Event(103, 'blackbox', 'runtime-log', rule='blackbox_saved')]
    run = {'id': 'run', 'start': 100}
    data = analysis_data(run, events, None)
    assert [e['issue'] for e in data['events']] == [False, True, True]
    assert data['duration'] == 3
    page = render_analysis(run, events, None)
    assert malicious not in page
    assert '\\u003c/script\\u003e' in page
    assert 'range-apply' in page and 'evidence-select' in page


def test_index_retains_anomalies_and_handles_empty_unknown_usage():
    run = {'id': 'special', 'start': 1, 'scope': '<script>bad</script>', 'result': 'ok', 'anomalies': {'drive.feedback': 2}}
    page = render_home({'runs': [run]})
    assert '2</strong><span>异常计数' in page
    assert '<script>bad</script>' not in page
    assert '域' in page and 'CPU%' in page and 'failed_stage' not in page
    assert '未知' in page
    assert 'http-equiv="refresh"' not in page
    assert '等待第一份运行记录' in render_home({'runs': []})


@pytest.mark.parametrize('result', ['running', 'failed', 'ok', 'interrupted'])
def test_home_only_shows_latest_run_even_after_it_stops(result):
    latest = {'id': 'latest', 'start': 20, 'result': result, 'anomalies': {'drive.feedback': 2},
              'failure_label': '本次故障', 'blackbox_saved': 1, 'experiments': 3}
    old = {'id': 'old', 'start': 10, 'result': 'failed', 'failure_label': '过去的故障',
           'anomalies': {'drive.feedback': 100}, 'blackbox_saved': 99}
    page = render_home({'runs': [old, latest]})  # Do not rely on index ordering.
    current = page.split('<div id="current-run">', 1)[1].split('<section id="history"', 1)[0]
    assert '本次故障' in current and '过去的故障' not in current
    assert '/latest/' in current and '/old/' not in current
    assert '2</strong><span>异常计数' in current
    assert '1</strong><span>触发窗口' in current
    assert '3</strong><span>数据记录' in current
    assert '过去的故障' in page  # Still available in history.
    assert 'class="history-section" hidden' in page


def test_new_clean_run_does_not_inherit_old_faults():
    page = render_home({'runs': [
        {'id': 'old', 'start': 1, 'result': 'failed', 'failure_label': '过去的故障'},
        {'id': 'new', 'start': 2, 'result': 'running'}]})
    current = page.split('<div id="current-run">', 1)[1].split('<section id="history"', 1)[0]
    assert '本次运行暂无异常记录' in current
    assert '过去的故障' not in current
    assert 'incident-row' not in current
    assert '/new/' in current


def test_stored_report_is_upgraded_without_modifying_archive_and_http_remains_read_only(evidence):
    root, run, _, origin = evidence
    old = '<html>old report</html>'
    (run / 'report.html').write_text(old)
    server = serve(Archive(root), '127.0.0.1', 0)
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        page = urlopen(base + '/run/').read().decode()
        assert '故障分析' in page and 'runtime-wave-range' in page and '全部事件与原始文件' in page
        assert (run / 'report.html').read_text() == old
        query = f'/run/blackbox/window/waveform.json?joint=wheel&start=10.002&end=10.006&origin={origin}'
        assert json.load(urlopen(base + query))['samples'] == 5
        with pytest.raises(HTTPError) as exc:
            urlopen(Request(base + '/run/', method='POST', data=b'{}'))
        assert exc.value.code == 405
        with pytest.raises(HTTPError) as exc:
            urlopen(base + '/run/blackbox/window/waveform.json?joint=wheel&origin=nan')
        assert exc.value.code == 400
    finally:
        server.shutdown(); server.server_close()
