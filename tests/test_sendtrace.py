import shutil
from pathlib import Path

from runtime_log.sendtrace import read_send_trace

FIXTURE = Path(__file__).resolve().parent / "fixtures/send_trace/send_trace.csv"


def test_clean_bench_start_has_no_burst():
    trace = read_send_trace(FIXTURE)
    control = trace["phases"]["control"]
    assert control["samples"] == 48868 and control["min_gap_ns"] == 584850
    assert control["gaps_below_burst"] == 0 and trace["burst"] is False
    assert trace["phases"]["maintenance"]["samples"] == 2
    window = trace["handoff_window"]
    assert [w["phase"] for w in window[:5]].count("startup") >= 3 and window[5]["phase"] == "control" and window[5]["t_us"] == 0


def test_burst_is_detected(tmp_path):
    for suffix in ("", ".stats.csv", ".hist.csv"):
        shutil.copy(f"{FIXTURE}{suffix}", tmp_path / f"send_trace.csv{suffix}")
    hist = tmp_path / "send_trace.csv.hist.csv"
    hist.write_text(hist.read_text() + "control,12,1400\n")
    trace = read_send_trace(tmp_path / "send_trace.csv")
    assert trace["burst"] is True and trace["phases"]["control"]["gaps_below_burst"] == 1400


def test_missing_trace_is_none(tmp_path):
    assert read_send_trace(tmp_path / "nothing.csv") is None



def test_find_send_trace_picks_the_file_written_during_the_run(tmp_path):
    import os

    from runtime_log.sendtrace import find_send_trace

    old, run, other = tmp_path / "a/send_trace.csv", tmp_path / "b/send_trace.csv", tmp_path / "c/other.csv"
    for path, mtime in ((old, 1000.0), (run, 2050.0), (other, 2060.0)):
        path.parent.mkdir()
        path.write_text("x")
        os.utime(path, (mtime, mtime))
    assert find_send_trace(str(tmp_path / "*/send_trace.csv"), 2000.0, 2040.0) == run
    assert find_send_trace(str(tmp_path / "*/send_trace.csv"), 3000.0, 3100.0) is None
