import json
import os
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
    write_json(archive.root / "r1" / "run.json", {"id": "r1", "result": "running", "control_pid": os.getpid(), "scope": "bench", "domain": "77"})
    write_json(archive.root / "r0" / "run.json", {"id": "r0", "result": "ok", "control_pid": 4241})
    recorder = PyWriter(shm / f"runtime-log.{os.getpid()}.runtime_log_recorder", FIELDS)
    recorder.write([0.5, 0.501, 0x1637, 0, 0.1, 3, 8])
    recorder.write([0.5, 0.502, 0x1637, 0xA000, 0.1, 3, 4])
    manager = PyWriter(shm / f"runtime-log.{os.getpid()}.enable_manager", ["phase", "owner", "last_failure_stage"])
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


def test_joint_channels_do_not_leak_into_scalar_values(profile):
    from runtime_log.live import decode

    decoded = decode({"left_joint1/position": 0.1, "left_joint1/target_position": 0.1, "phase": 2.0, "owner": 1.0}, profile.blackbox["enums"])
    assert decoded["values"] == {"owner": 1.0}
    assert decoded["joints"][0]["joint"] == "left_joint1"


def test_force_sensors_are_separate_from_joints_and_preserve_raw_values():
    from runtime_log.live import decode, force_sensor_rows

    fields = {f"left_wrist_force_sensor/channel_{i}_raw": value
              for i, value in enumerate([-2147483648, 2147483647, 3, -4, 5, 6], 1)}
    fields.update({"left_wrist_force_sensor/sample_counter_raw": 4294967295,
                   "left_wrist_force_sensor/status_code_raw": 0,
                   "left_wrist_force_sensor/temperature_raw": 0,
                   "left_joint1/position": 0.1, "ethercat_slave_8/al_state": 8})
    decoded = decode(fields, {})
    assert [j["joint"] for j in decoded["joints"]] == ["left_joint1"]
    segment = {**decoded, "age_s": 0.01, "state": "active", "writer_alive": True}
    left, right = force_sensor_rows([segment])
    assert left["raw"] == [-2147483648, 2147483647, 3, -4, 5, 6]
    assert left["converted"] == [-21474.83648, 21474.83647, 0.00003, -0.00004, 0.00005, 0.00006]
    assert left["units"] == ["N", "N", "N", "N·m", "N·m", "N·m"]
    assert right["converted"] == [None] * 6
    assert left["sample_counter"] == 4294967295
    assert left["status"] == "周期记录更新"
    assert right["status"] == "未接入采集" and right["raw"] == [None] * 6
    for change, status in [({"age_s": 2}, "记录已过期"),
                           ({"writer_alive": False}, "记录已过期"),
                           ({"state": "inactive"}, "记录已停用"),
                           ({"bus": {"al_states": {"8": "SAFEOP"}}}, "总线未就绪")]:
        assert force_sensor_rows([{**segment, **change}])[0]["status"] == status
    fields["left_wrist_force_sensor/channel_1_raw"] = float("nan")
    invalid = {**segment, **decode(fields, {})}
    assert force_sensor_rows([invalid])[0]["status"] == "读数不完整"
    assert force_sensor_rows([invalid])[0]["converted"][0] is None
    json.dumps(force_sensor_rows([invalid]), allow_nan=False)


def test_snapshot_includes_force_readings_and_missing_side(tmp_path):
    archive = Archive(tmp_path / "runs")
    write_json(archive.root / "r1" / "run.json", {"id": "r1", "result": "running", "control_pid": os.getpid()})
    fields = [f"right_wrist_force_sensor/channel_{i}_raw" for i in range(1, 7)]
    recorder = PyWriter(tmp_path / f"runtime-log.{os.getpid()}.force", fields)
    recorder.write([1, -2, 3, -4, 5, -6])
    left, right = snapshot(archive, {}, tmp_path)["runs"][0]["force_sensors"]
    assert left["status"] == "未接入采集"
    assert right["raw"] == [1, -2, 3, -4, 5, -6]
    assert right["converted"] == [0.00001, -0.00002, 0.00003, -0.00004, 0.00005, -0.00006]


def test_exited_control_process_is_not_shown_even_with_old_segments(tmp_path, monkeypatch):
    archive = Archive(tmp_path / "runs")
    for pid in (4241, 4242):
        write_json(archive.root / str(pid) / "run.json",
                   {"id": str(pid), "result": "running", "control_pid": pid})
        writer = PyWriter(tmp_path / f"runtime-log.{pid}.recorder", FIELDS)
        writer.write([0.5, 0.5, 64, 0, 0, 0, 8])
    def alive(pid, signal):
        if pid == 4241:
            raise ProcessLookupError()
    monkeypatch.setattr("runtime_log.live.os.kill", alive)
    assert [r["id"] for r in snapshot(archive, {}, tmp_path)["runs"]] == ["4242"]
    monkeypatch.setattr("runtime_log.live._process_alive", lambda pid: False)
    data = snapshot(archive, {}, tmp_path)
    assert data["runs"] == []
    assert len(data["force_sensors"]) == 2
    assert json.loads((archive.root / "4241/run.json").read_text())["result"] == "running"


def test_bluepoint_protocol_unit_conversion_handles_zero_missing_and_infinity():
    from runtime_log.live import decode

    values = [100000, -100000, 0, 250000, float("inf")]
    fields = {f"left_wrist_force_sensor/channel_{i}_raw": value
              for i, value in enumerate(values, 1)}
    sensor = decode(fields, {})["force_sensors"][0]
    assert sensor["raw"] == [100000, -100000, 0, 250000, None, None]
    assert sensor["converted"] == [1.0, -1.0, 0.0, 2.5, None, None]
    json.dumps(sensor, allow_nan=False)
