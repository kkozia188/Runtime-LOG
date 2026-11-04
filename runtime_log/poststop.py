"""After a run: read each drive's error code (0x603F) and status word (0x6041) once over SDO.

Only when the master is no longer in Operation (the application released it) and the slave is
still on the bus. One attempt per object, no retries; failures are recorded with their reason.
"""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Optional

import yaml

from .ethercat import EthercatPoller


def artifact_fault_names(generated_dir) -> dict:
    """Vendor code -> text from the artifact's diagnostics.yaml fault tables (robot_driver owns them)."""
    path = Path(generated_dir) / "diagnostics.yaml" if generated_dir else None
    if path is None or not path.is_file():
        return {}
    tables = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("fault_tables", {})
    return {
        int(fault["code"], 16): f"{fault['message']}（{table['vendor']} 手册 p{fault['page']}）"
        for table in tables.values()
        for fault in table.get("faults", [])
        if "code" in fault
    }


def read_post_stop(poller: EthercatPoller, settings: dict, generated_dir=None) -> dict:
    result: dict = {"time": time.time(), "drives": [], "skipped": None}
    try:
        master = poller.master_state()
    except Exception as error:  # noqa: BLE001 - recorded, never raised
        result["skipped"] = f"ethercat master failed: {error}"[:200]
        return result
    result["master_phase"] = master.get("phase")
    if master.get("phase") == "Operation" or master.get("active"):
        result["skipped"] = f"master still in {master.get('phase')} (active={master.get('active')}); another application holds it"
        return result
    try:
        slaves = poller.slave_states()
    except Exception as error:  # noqa: BLE001
        result["skipped"] = f"ethercat slaves failed: {error}"[:200]
        return result
    pattern = re.compile(settings.get("drive_name_regex", "Driver|Drive|Servo"))
    names = artifact_fault_names(generated_dir)
    for slave in slaves:
        if not pattern.search(slave["name"]):
            continue
        row: dict = {"position": slave["pos"], "name": slave["name"], "al_state": slave["state"]}
        for key, index in (("error_code", 0x603F), ("status_word", 0x6041)):
            try:
                row[key] = poller.read_sdo(slave["pos"], index)
            except Exception as error:  # noqa: BLE001
                row[f"{key}_error"] = str(error)[:160]
        code = row.get("error_code")
        if code is not None:
            row["error_code_hex"] = f"0x{code:04X}"
            row["error_text"] = names.get(code, "")
        if row.get("status_word") is not None:
            row["status_word_hex"] = f"0x{row['status_word']:04X}"
            row["fault"] = bool(row["status_word"] & 0x08)
        result["drives"].append(row)
    return result


def summarize(result: Optional[dict], expected_codes=()) -> dict:
    """Anomaly counts: drives with a non-zero code or Fault after the run (expected codes as reference only)."""
    counts: dict = {}
    expected = {int(code) for code in expected_codes}
    for row in (result or {}).get("drives", []):
        code = row.get("error_code")
        if code:
            key = "shutdown.drive.expected_code_after_stop" if code in expected else "drive.error_code_after_stop"
            counts[key] = counts.get(key, 0) + 1
        if row.get("fault"):
            counts["drive.fault_after_stop"] = counts.get("drive.fault_after_stop", 0) + 1
        if any(key.endswith("_error") for key in row):
            counts["drive.post_stop_read_failed"] = counts.get("drive.post_stop_read_failed", 0) + 1
    return counts
