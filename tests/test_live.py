import json
import urllib.request

from runtime_log.archive import Archive, write_json
from runtime_log.live import snapshot
from runtime_log.web import serve
from test_blackbox import PyWriter

FIELDS = ["left_joint1/position", "left_joint1/target_position", "left_joint1/status_word", "left_joint1/error_code",
          "left_joint1/velocity", "ethercat_master/wc_error_count", "ethercat_slave_1/al_state"]


def test_snapshot_decodes_the_latest_cycle_of_running_runs(tmp_path, profile):
    shm = tmp_path / "shm"
    shm.mkdir()
    archive = Archive(tmp_path / "runs")
    write_json(archive.root / "r1" / "run.json", {"id": "r1", "result": "running", "control_pid": 4242, "scope": "bench", "domain": "77"})
    write_json(archive.root / "r0" / "run.json", {"id": "r0", "result": "ok", "control_pid": 4241})
    recorder = PyWriter(shm / "runtime-log.4242.runtime_log_recorder", FIELDS)
    recorder.write([0.5, 0.501, 0x1637, 0, 0.1, 3, 8])
    recorder.write([0.5, 0.502, 0x1637, 0xA000, 0.1, 3, 4])
    manager = PyWriter(shm / "runtime-log.4242.enable_manager", ["phase", "owner", "last_failure_stage"])
    manager.write([6, 1, 0])
    data = snapshot(archive, profile.blackbox["enums"], shm)
    assert [run["id"] for run in data["runs"]] == ["r1"]
    rows = [s for s in data["runs"][0]["segments"] if s["joints"]][0]
    joint = rows["joints"][0]
    assert joint["state"] == "运行使能" and joint["power"] is True and joint["error_code"] == "0xA000"
    assert abs(joint["error_deg"] - 0.002 * 57.29578) < 1e-3
    assert rows["bus"]["wc_error_count"] == 3 and rows["bus"]["al_states"] == {"1": "SAFEOP"}
    states = [s for s in data["runs"][0]["segments"] if s["states"]][0]["states"]
    assert states["phase"]["label"] == "已使能" and states["last_failure_stage"]["name"] == "success"


def test_live_routes(tmp_path):
    archive = Archive(tmp_path / "runs")
    server = serve(archive, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert "实时状态" in urllib.request.urlopen(base + "/live").read().decode()
        assert json.loads(urllib.request.urlopen(base + "/live.json").read())["runs"] == []
    finally:
        server.shutdown()
