"""Black box triggers, expected transitions, window saving (MCAP) and writer liveness."""

import json
import os
import struct
from pathlib import Path

import pytest

from runtime_log.blackbox import BlackBox, Settings
from runtime_log.shm import ShmReader

FIELDS = ["left_joint1/position", "left_joint1/status_word", "ethercat_slave_1/al_state",
          "ethercat_master/wc_error_count", "ethercat_domain/process_data_age_ms"]


class PyWriter:
    """Writes the v1 segment format exactly like shm_ring.hpp (single thread, for tests)."""

    def __init__(self, path: Path, fields, slots=4000, pid=None):
        self.path, self.fields, self.slots = path, fields, slots
        self.header_size = ((320 + 64 * len(fields) + 4095) // 4096) * 4096
        self.slot_size = ((24 + 8 * len(fields) + 63) // 64) * 64
        data = bytearray(self.header_size + slots * self.slot_size)
        struct.pack_into("<8sIIIIIIqqq", data, 0, b"RTLOGSHM", 1, self.header_size, slots, self.slot_size, len(fields), 1,
                         pid or os.getpid(), 0, 0)
        data[128:128 + 4] = b"test"
        for i, name in enumerate(fields):
            base = 320 + 64 * i
            data[base:base + len(name)] = name.encode()
            struct.pack_into("<IB", data, base + 56, 24 + 8 * i, 1)
        path.write_bytes(bytes(data))
        self.next = 1

    def write(self, values, time_ns=None, period_ns=1_000_000):
        seq = self.next
        time_ns = time_ns if time_ns is not None else 1_800_000_000_000_000_000 + seq * 1_000_000
        offset = self.header_size + (seq % self.slots) * self.slot_size
        with self.path.open("r+b") as stream:
            stream.seek(offset)
            stream.write(struct.pack("<Qqq", seq, time_ns, period_ns) + struct.pack(f"<{len(values)}d", *values))
            stream.seek(64)
            stream.write(struct.pack("<Q", seq))
        self.next += 1

    def state(self, value: int):
        with self.path.open("r+b") as stream:
            stream.seek(28)
            stream.write(struct.pack("<I", value))


def run_cycles(writer, count, sw=0x1637, al=8, wkc=0, age=0.0, period_ns=1_000_000):
    for i in range(count):
        writer.write([0.1 * i, sw, al, wkc, age], period_ns=period_ns)


def make_box(tmp_path, writer_path, explain=None, **settings):
    events = []
    box = BlackBox(writer_path, tmp_path / "bb", Settings(**{"pre_s": 1.0, "post_s": 0.5, "stall_s": 3600, **settings}),
                   explain=explain, kernel_lines=lambda a, b: ["kernel line"], on_event=events.append)
    return box, events


def test_drive_fault_saves_a_window_with_pre_and_post_cycles(tmp_path):
    pytest.importorskip("mcap")
    writer = PyWriter(tmp_path / "seg", FIELDS)
    box, events = make_box(tmp_path, writer.path)
    run_cycles(writer, 2000)
    box._step(final=False)
    run_cycles(writer, 1, sw=0x0218)  # Fault set, left Operation Enabled
    run_cycles(writer, 300, sw=0x0218)
    box._step(final=False)
    assert box.pending is not None and not box.saved  # still waiting for the 0.5 s after the trigger
    run_cycles(writer, 300, sw=0x0218)
    box._step(final=False)
    assert len(box.saved) == 1 and events[-1]["kind"] == "blackbox_saved"
    assert set(events[-1]["reasons"]) == {"drive_fault", "left_operation_enabled"}
    target = tmp_path / "bb" / box.saved[0]
    document = json.loads((target / "trigger.json").read_text())
    assert document["window"]["first_seq"] == 2001 - 1000 and document["window"]["last_seq"] == 2001 + 500
    assert document["window"]["missing"] == 0
    assert (target / "kernel.log").read_text().startswith("kernel line")

    from mcap.reader import make_reader

    with (target / "window.mcap").open("rb") as stream:
        messages = [json.loads(m.data) for _, _, m in make_reader(stream).iter_messages()]
    assert len(messages) == 1501
    assert messages[1000]["seq"] == 2001 and messages[1000]["left_joint1/status_word"] == 0x0218
    assert messages[0]["period_us"] == 1000.0


def test_disable_request_is_an_expected_transition_and_saves_nothing(tmp_path):
    writer = PyWriter(tmp_path / "seg", FIELDS)
    box, events = make_box(tmp_path, writer.path, explain=lambda start, end: "/rt/disable")
    run_cycles(writer, 1500)
    box._step(final=False)
    run_cycles(writer, 1000, sw=0x1250)  # Switch On Disabled, no fault
    box._step(final=False)
    assert not box.saved and events[-1]["kind"] == "blackbox_expected"
    assert events[-1]["reasons"] == ["left_operation_enabled"]


def test_bus_triggers_and_cycle_statistics(tmp_path):
    pytest.importorskip("mcap")
    writer = PyWriter(tmp_path / "seg", FIELDS)
    box, events = make_box(tmp_path, writer.path)
    run_cycles(writer, 1500)
    run_cycles(writer, 1, wkc=1, age=5.0, al=4, period_ns=4_000_000)
    run_cycles(writer, 600, wkc=1, age=5.0, al=4)
    box._step(final=True)
    assert set(events[-1]["reasons"]) == {"wkc_error", "process_data_stale", "slave_left_op", "cycle_overrun"}
    stats = box.describe()["cycle_stats"]
    assert stats["cycles"] == 2101 and stats["overruns"] == 1 and stats["max_period_ns"] == 4_000_000
    assert stats["nominal_period_ns"] == 1_000_000


def test_writer_that_stops_while_active_is_a_trigger(tmp_path):
    pytest.importorskip("mcap")
    writer = PyWriter(tmp_path / "seg", FIELDS)
    box, events = make_box(tmp_path, writer.path, stall_s=0.0)
    run_cycles(writer, 1500)
    box._step(final=False)
    box._step(final=True)  # head did not move and the segment still says "active"
    assert events and events[-1]["reasons"] == ["writer_stalled"]


def test_external_trigger_maps_epoch_time_to_the_cycle(tmp_path):
    pytest.importorskip("mcap")
    writer = PyWriter(tmp_path / "seg", FIELDS)
    box, events = make_box(tmp_path, writer.path)
    run_cycles(writer, 3000)
    box._step(final=False)
    box.trigger("kernel_error", 1_800_000_000.0 + 2.0005, "EtherCAT ERROR 0-3: ...")
    box._step(final=True)
    document = json.loads((tmp_path / "bb" / box.saved[0] / "trigger.json").read_text())
    assert document["triggers"][0]["seq"] == 2000 and document["reasons"] == ["kernel_error"]


def test_power_loss_and_quick_stop_are_triggers_and_states_are_decoded(tmp_path):
    pytest.importorskip("mcap")
    from runtime_log import cia402

    assert cia402.state(0x1637) == "operation_enabled" and cia402.decode(0x1637)["voltage_enabled"]
    assert cia402.state(0x1250) == "switch_on_disabled" and cia402.state(0x0218) == "fault"
    assert cia402.state(0x1617) == "quick_stop_active"
    writer = PyWriter(tmp_path / "seg", FIELDS)
    box, events = make_box(tmp_path, writer.path)
    run_cycles(writer, 1500)
    run_cycles(writer, 700, sw=0x1627)  # main power gone, still reporting Operation Enabled
    box._step(final=False)
    assert events[-1]["reasons"] == ["power_lost"]
    run_cycles(writer, 1500, sw=0x1617)
    box._step(final=True)
    assert set(events[-1]["reasons"]) == {"quick_stop", "left_operation_enabled"}
    states = box.describe()["drive_states"]
    assert states["left_joint1"]["state"] == "quick_stop_active" and states["left_joint1"]["label"] == "快速停机"


def test_enable_manager_trace_becomes_state_events_and_emergency_saves_a_window(tmp_path, profile):
    pytest.importorskip("mcap")
    fields = ["phase", "owner", "last_failure_stage"]
    writer = PyWriter(tmp_path / "em", fields)
    events = []
    box = BlackBox(writer.path, tmp_path / "bb", Settings(pre_s=1, post_s=0.2, stall_s=3600, enums=profile.blackbox["enums"]),
                   on_event=events.append)
    for phase, stage, count in ((2, 0, 1200), (3, 0, 300), (4, 0, 200), (6, 0, 500), (13, 13, 400)):
        for _ in range(count):
            writer.write([phase, 1, stage])
    box._step(final=True)
    changes = [(e["field"], e["old_label"], e["new_label"]) for e in events if e["kind"] == "state_change"]
    assert changes == [("phase", "空闲", "使能中"), ("phase", "使能中", "批间等待"), ("phase", "批间等待", "已使能"),
                       ("phase", "已使能", "紧急快停"), ("last_failure_stage", "success", "fault_detected")]
    saved = [e for e in events if e["kind"] == "blackbox_saved"]
    assert saved and set(saved[0]["reasons"]) == {"phase:emergency_quick_stop", "last_failure_stage:fault_detected"}


def test_reader_rejects_foreign_files(tmp_path):
    path = tmp_path / "junk"
    path.write_bytes(b"x" * 8192)
    with pytest.raises(ValueError):
        ShmReader(path)


CHASSIS = ["wheel1_joint/status_word", "wheel1_joint/mode_request_error", "wheel1_joint/feedback_age_ms",
           "steering_encoder_1/feedback_age_ms", "steering_encoder_1/ready"]


def chassis_cycles(writer, count, error=0.0, wheel_age=1.0, encoder_age=4.0, ready=1.0):
    for _ in range(count):
        writer.write([0x1637, error, wheel_age, encoder_age, ready])


def test_profile_interface_triggers_on_chassis_fields(tmp_path, profile):
    pytest.importorskip("mcap")
    writer = PyWriter(tmp_path / "seg", CHASSIS)
    box, events = make_box(tmp_path, writer.path, interface_triggers=profile.blackbox["interface_triggers"])
    chassis_cycles(writer, 1500)
    box._step(final=False)
    chassis_cycles(writer, 1, error=1.0, wheel_age=35.0, encoder_age=25.0, ready=0.0)
    chassis_cycles(writer, 600, error=1.0, wheel_age=35.0, encoder_age=25.0, ready=0.0)
    box._step(final=True)
    assert events[-1]["kind"] == "blackbox_saved"
    assert set(events[-1]["reasons"]) == {"drive_adapter_error", "feedback_stale", "sensor_not_ready"}
    document = json.loads((tmp_path / "bb" / box.saved[0] / "trigger.json").read_text())
    details = sorted(t["detail"] for t in document["triggers"] if t["reason"] == "feedback_stale")
    assert details == ["steering_encoder_1/feedback_age_ms 4 -> 25", "wheel1_joint/feedback_age_ms 1 -> 35"]


def test_interface_triggers_are_edges_and_shutdown_explains_them(tmp_path, profile):
    writer = PyWriter(tmp_path / "seg", CHASSIS)
    box, events = make_box(tmp_path, writer.path, explain=lambda a, b: "shutdown",
                           interface_triggers=profile.blackbox["interface_triggers"])
    chassis_cycles(writer, 1500, encoder_age=30.0)  # already stale at the first cycle: no edge
    box._step(final=False)
    assert box.pending is None
    chassis_cycles(writer, 1, encoder_age=4.0)
    chassis_cycles(writer, 1, encoder_age=30.0)  # crosses again during a stop
    chassis_cycles(writer, 600, encoder_age=30.0)
    box._step(final=True)
    assert not box.saved and events[-1]["kind"] == "blackbox_expected" and events[-1]["reasons"] == ["feedback_stale"]


def test_bad_interface_trigger_is_rejected(tmp_path):
    writer = PyWriter(tmp_path / "seg", CHASSIS)
    with pytest.raises(ValueError):
        make_box(tmp_path, writer.path, interface_triggers=[{"interface": "ready", "when": "below", "threshold": 1,
                                                             "reason": "x"}])
