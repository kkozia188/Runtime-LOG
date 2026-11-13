import json
import urllib.error
import urllib.request

import pytest

from runtime_log.anomalies import cycle, snapshot
from runtime_log.archive import Archive, write_json
from runtime_log.web import serve

STATS = {"cycles": 3396, "overruns": 1, "max_period_ns": 2000872, "min_period_ns": 995046,
         "mean_period_ns": 1000293.9, "nominal_period_ns": 999910}


def _window(archive, run_id, name, time_s, reason, detail):
    write_json(archive.root / run_id / "blackbox" / name / "trigger.json", {
        "reasons": [reason], "trigger_count": 1,
        "triggers": [{"seq": 1, "time_ns": int(time_s * 1e9), "reason": reason, "detail": detail}],
        "window": {"start": time_s - 10, "end": time_s + 2},
    })


@pytest.fixture
def archive(tmp_path):
    archive = Archive(tmp_path / "runs")
    write_json(archive.root / "old" / "run.json", {"id": "old", "result": "ok", "start": 10.0})
    write_json(archive.root / "r1" / "run.json", {
        "id": "r1", "result": "running", "start": 100.0, "scope": "bench_dual_arm_head", "domain": "196",
        "blackbox": [
            {"segment": "runtime-log.1.runtime_log_recorder", "cycle_stats": dict(STATS, cycles=2537, overruns=0)},
            {"segment": "runtime-log.1.enable_manager", "cycle_stats": STATS},
        ],
        "summary": {"anomalies": {"cycle.overruns": 1, "ecat.wkc_errors": 2}},
    })
    _window(archive, "r1", "20261111-120000.000-drive_fault", 200.0, "drive_fault", "right_joint4 Operation enabled -> Fault")
    _window(archive, "r1", "20261111-120100.000-wkc_error", 260.0, "wkc_error", "wc_error_count 3 -> 5")
    return archive


def test_cycle_uses_the_segment_with_most_cycles_and_reports_the_worst_deviation():
    data = cycle([{"segment": "a", "cycle_stats": dict(STATS, cycles=10, overruns=9)},
                  {"segment": "b", "cycle_stats": STATS}])
    assert data["segment"] == "b" and data["overruns"] == 1 and data["cycles"] == 3396
    assert data["nominal_period_us"] == 999.91
    assert data["max_jitter_us"] == 1001.0  # 2000872 - 999910 ns
    assert data["overrun_rule"] == "period > 1.5 x nominal"


def test_cycle_without_statistics_is_none():
    assert cycle([]) is None
    assert cycle([{"segment": "a", "cycle_stats": {"cycles": 5, "nominal_period_ns": None}}]) is None


def test_snapshot_lists_running_runs_with_windows_newest_first(archive):
    data = snapshot(archive)
    assert [run["id"] for run in data["runs"]] == ["r1"]
    run = data["runs"][0]
    assert run["running"] is True and run["report_url"] == "/r1/"
    assert [w["reasons"] for w in run["blackbox"]] == [["wkc_error"], ["drive_fault"]]
    fault = run["blackbox"][1]
    assert fault["subjects"] == ["right_joint4"] and fault["time"] == 200.0
    assert fault["url"] == "/r1/blackbox/20261111-120000.000-drive_fault/"
    assert run["cycle"]["overruns"] == 1
    # The statistics are cumulative and come from run.json, refreshed about every 5 s.
    assert run["running"] is True and run["update_age_s"] is not None
    assert run["cycle"]["overrun_rule"] == "period > 1.5 x nominal"


def test_since_filters_windows(archive):
    run = snapshot(archive, since=250.0)["runs"][0]
    assert [w["reasons"] for w in run["blackbox"]] == [["wkc_error"]]


def test_without_a_running_run_the_latest_run_is_reported(tmp_path):
    archive = Archive(tmp_path / "runs")
    write_json(archive.root / "a" / "run.json", {"id": "a", "result": "ok", "start": 1.0})
    write_json(archive.root / "b" / "run.json", {"id": "b", "result": "failed", "start": 2.0})
    runs = snapshot(archive)["runs"]
    assert [(r["id"], r["running"], r["cycle"], r["blackbox"]) for r in runs] == [("b", False, None, [])]


def test_route(archive):
    server = serve(archive, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        data = json.loads(urllib.request.urlopen(base + "/anomalies.json?since=250").read())
        assert len(data["runs"][0]["blackbox"]) == 1 and data["since"] == 250.0
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + "/anomalies.json?since=-1")
        assert error.value.code == 400
    finally:
        server.shutdown()
