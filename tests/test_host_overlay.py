"""Per-IPC host overlay of the project profile and the station header of the index page."""

from pathlib import Path

from runtime_log.profile import load_profile, merge
from runtime_log.web import render_index

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "profiles/rt_control.yaml"


def test_merge_replaces_lists_and_recurses_into_mappings():
    base = {"a": {"x": 1, "y": [1, 2]}, "b": [1], "c": 3}
    assert merge(base, {"a": {"y": [9]}, "b": [], "d": 4}) == {"a": {"x": 1, "y": [9]}, "b": [], "c": 3, "d": 4}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": [1], "c": 3}  # input untouched


def test_missing_host_file_keeps_the_profile(tmp_path):
    plain = load_profile(PROFILE)
    same = load_profile(PROFILE, tmp_path / "absent.yaml")
    assert same.git_repos == plain.git_repos and same.ethercat == plain.ethercat and same.station == {}


def test_example_chassis_overlay():
    profile = load_profile(PROFILE, ROOT / "deploy/host.example.yaml")
    plain = load_profile(PROFILE)
    assert profile.git_repos == ["~/Robot/rt-control/v3/robot_driver"]
    post = profile.ethercat["post_stop"]
    assert post["enabled"] is True and "FD Drive" in post["drive_name_regex"] and post["expected_codes"] == []
    assert profile.ethercat["steady_poll_hz"] == plain.ethercat["steady_poll_hz"]  # not overridden
    assert [r.id for r in profile.rules] == [r.id for r in plain.rules]  # project rules unchanged
    assert profile.station["name"].startswith("底盘台架")


def test_index_shows_station_name_and_only_http_peer_links():
    station = {"name": "底盘 <台架>", "peers": [{"name": "机械臂", "url": "http://192.168.100.40:18200/"},
                                             {"name": "bad", "url": "javascript:alert(1)"}]}
    page = render_index({"runs": []}, station)
    assert "底盘 &lt;台架&gt;" in page and 'href="http://192.168.100.40:18200/"' in page
    assert "javascript:" not in page
    assert "其他工控机" not in render_index({"runs": []})
