import json
import math
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from mcap.writer import Writer

from runtime_log.archive import Archive
from runtime_log.waveform import waveform
from runtime_log.web import serve


@pytest.fixture
def recorded(tmp_path):
    path = tmp_path / 'data.mcap'
    with path.open('wb') as stream:
        writer = Writer(stream)
        writer.start()
        channel = writer.register_channel('/cycles', 'json', 0)
        for i in range(10000):
            if i == 7001:
                continue
            row = {'seq': i, 'period_us': 8300 if i == 1234 else 1000,
                   'left_joint1/position': math.nan if i == 4000 else 0.1,
                   'left_joint1/target_position': 0.2,
                   'left_joint1/velocity': 0.3, 'left_joint1/torque_permille': -10,
                   'left_joint1/status_word': 0x1637}
            writer.add_message(channel, log_time=1800000000000000000 + i * 1000000,
                               publish_time=1800000000000000000 + i * 1000000,
                               data=json.dumps(row).encode())
        writer.finish()
    return path


def test_overview_preserves_peak_units_and_gaps(recorded):
    data = waveform(recorded, 'left_joint1', width=100)
    signals = {s['key']: s for s in data['series']}
    assert data['samples'] == 9999 and data['missing'] == 1 and data['aggregated']
    assert data['duration'] == 9.999
    assert max(p[1] for p in signals['period_us']['points'] if p[1] is not None) == 8300
    assert signals['position']['points'][0][1] == pytest.approx(math.degrees(0.1))
    assert signals['error']['points'][0][1] == pytest.approx(math.degrees(0.1))
    assert any(p[1] is None for p in signals['position']['points'])
    assert any(p[1] is None for p in signals['period_us']['points'])
    assert len(signals['position']['points']) <= 410
    json.dumps(data, allow_nan=False)


def test_zoom_reads_original_samples_and_includes_endpoint(recorded):
    data = waveform(recorded, 'left_joint1', start=1.230, end=1.240)
    assert data['samples'] == 11 and not data['aggregated']
    period = next(s for s in data['series'] if s['key'] == 'period_us')
    assert [1.234, 8300] in period['points']
    assert period['points'][-1][0] == 1.240


@pytest.mark.parametrize('kwargs', [{'joint': 'bad'}, {'start': -1}, {'start': float('nan')},
                                    {'end': float('inf')}, {'start': 2, 'end': 1}, {'start': 20}])
def test_reject_invalid_queries(recorded, kwargs):
    with pytest.raises(ValueError):
        waveform(recorded, **({'joint': 'left_joint1'} | kwargs))


def test_waveform_http_and_old_experiment_page(tmp_path, recorded):
    root = tmp_path / 'archive'
    exp = root / 'run' / 'experiments' / 'goal-1'
    exp.mkdir(parents=True)
    recorded.rename(exp / 'data.mcap')
    (root / 'run' / 'run.json').write_text('{}')
    (exp / 'meta.json').write_text(json.dumps({'name': 'goal-1', 'joints': {'left_joint1': {}}}))
    server = serve(Archive(root), '127.0.0.1', 0)
    base = f'http://127.0.0.1:{server.server_address[1]}/run/experiments/goal-1/'
    try:
        with urlopen(base) as response:
            assert 'wave-load' in response.read().decode()
        with urlopen(base + 'waveform.json?joint=left_joint1&start=1.23&end=1.24') as response:
            assert json.load(response)['samples'] == 11
        with pytest.raises(HTTPError) as error:
            urlopen(base + 'waveform.json?joint=bad')
        assert error.value.code == 400
        with pytest.raises(HTTPError) as error:
            urlopen(base + 'waveform.json?joint=left_joint1&width=bad')
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
