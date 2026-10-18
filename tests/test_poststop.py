from pathlib import Path

import pytest

from runtime_log.ethercat import EthercatPoller
from runtime_log.poststop import read_post_stop, summarize

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SETTINGS = {"drive_name_regex": "ZeroErr", "error_codes": {0xA000: "master left OP"}, "expected_codes": [0xA000]}


def poller(phase: str, codes: dict, fail=()):
    master = (FIXTURES / "ethercat_master_op.txt").read_text().replace("Phase: Operation", f"Phase: {phase}")
    if phase != "Operation":
        master = master.replace("Active: yes", "Active: no")
    slaves = (FIXTURES / "ethercat_slaves_op.txt").read_text().replace(" OP ", " PREOP ")
    calls = []

    def runner(argv):
        calls.append(argv)
        if argv[1] == "master":
            return master
        if argv[1] == "slaves":
            return slaves
        position, index = int(argv[argv.index("-p") + 1]), int(argv[-2], 16)
        if (position, index) in fail:
            raise RuntimeError("Failed to upload SDO: Operation not permitted")
        return {0x603F: f"0x{codes.get(position, 0):04x} {codes.get(position, 0)}\n", 0x6041: "0x0250 592\n"}[index]

    return EthercatPoller(runner=runner), calls


def test_reads_error_code_and_status_of_every_drive_once():
    instance, calls = poller("Idle", {1: 0xA000})
    result = read_post_stop(instance, SETTINGS)
    assert result["skipped"] is None and result["master_phase"] == "Idle"
    assert [d["position"] for d in result["drives"]] == [1, 2, 3, 4, 5, 6, 7]  # couplers skipped
    first = result["drives"][0]
    assert first["error_code_hex"] == "0xA000" and first["error_text"] == "master left OP"
    assert first["status_word_hex"] == "0x0250" and first["fault"] is False
    uploads = [argv for argv in calls if argv[1] == "upload"]
    assert len(uploads) == 14 and {argv[-2] for argv in uploads} == {"0x603F", "0x6041"}
    assert summarize(result, SETTINGS["expected_codes"]) == {"shutdown.drive.expected_code_after_stop": 1}


def test_unexpected_code_and_read_failure_are_anomalies():
    instance, _ = poller("Idle", {3: 0x7500}, fail={(5, 0x603F)})
    result = read_post_stop(instance, SETTINGS)
    assert summarize(result, SETTINGS["expected_codes"]) == {"drive.error_code_after_stop": 1, "drive.post_stop_read_failed": 1}
    assert "Operation not permitted" in result["drives"][4]["error_code_error"]


def test_skips_while_an_application_holds_the_master():
    instance, calls = poller("Operation", {})
    result = read_post_stop(instance, SETTINGS)
    assert "Operation" in result["skipped"] and not result["drives"]
    assert not [argv for argv in calls if argv[1] == "upload"]


def test_only_whitelisted_objects_can_be_read():
    instance, _ = poller("Idle", {})
    with pytest.raises(ValueError):
        instance.read_sdo(1, 0x6040)  # control word: never
    with pytest.raises(ValueError):
        instance.read_sdo(1, 0x603F, 1)
