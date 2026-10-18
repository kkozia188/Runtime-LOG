"""Read-only IgH `ethercat` CLI polling: master state/counters and per-slave AL state.

Only `master` and `slaves` are ever executed: both read the master's cached data through an
ioctl and send nothing on the bus. Any other sub-command is refused.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

ALLOWED = frozenset({"master", "slaves"})
# The only bus transaction Runtime-LOG ever makes: after a run, with the master Idle, read these objects.
SDO_READ_ALLOWED = {0x603F: "uint16", 0x6041: "uint16"}

_COUNTER_KEYS = {
    "Tx frames": "tx_frames",
    "Rx frames": "rx_frames",
    "Lost frames": "lost_frames",
    "Tx errors": "tx_errors",
    "Tx bytes": "tx_bytes",
    "Rx bytes": "rx_bytes",
}
SLAVE_LINE = re.compile(r"^\s*(?P<pos>\d+)\s+(?P<alias>\d+):(?P<rel>\d+)\s+(?P<state>\S+)\s+(?P<flag>[+E])\s+(?P<name>.*?)\s*$")


def parse_master(text: str) -> dict:
    """Phase/active/slave count, link, and counters (Common section preferred, Main as fallback)."""
    result: dict = {}
    section = ""
    common: dict = {}
    main: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("Main:"):
            section = "main"
            continue
        if line.startswith("Common:"):
            section = "common"
            continue
        if line.startswith("Distributed clocks"):
            section = "dc"
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "Phase":
            result["phase"] = value
        elif key == "Active":
            result["active"] = value == "yes"
        elif key == "Slaves":
            result["slaves"] = int(value) if value.isdigit() else None
        elif key == "Link":
            result["link"] = value
        elif key in _COUNTER_KEYS and section in ("main", "common"):
            target = common if section == "common" else main
            try:
                target[_COUNTER_KEYS[key]] = int(value.split()[0])
            except (ValueError, IndexError):
                pass
    counters = dict(main)
    counters.update(common)
    result["counters"] = counters
    return result


def parse_slaves(text: str) -> list[dict]:
    slaves = []
    for line in text.splitlines():
        match = SLAVE_LINE.match(line)
        if match:
            slaves.append(
                {
                    "pos": int(match["pos"]),
                    "alias": int(match["alias"]),
                    "state": match["state"],
                    "error": match["flag"] == "E",
                    "name": match["name"],
                }
            )
    return slaves


Runner = Callable[[list], str]


def _run(argv: list) -> str:
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=2.0, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"exit {completed.returncode}")
    return completed.stdout


class EthercatPoller:
    def __init__(self, master: int = 0, binary: str = "ethercat", runner: Optional[Runner] = None):
        self.master = master
        self.binary = binary
        self.runner = runner or _run

    def _call(self, command: str) -> str:
        if command not in ALLOWED:
            raise ValueError(f"refusing ethercat sub-command {command!r}: only read-only master/slaves")
        return self.runner([self.binary, command, "-m", str(self.master)])

    def master_state(self) -> dict:
        return parse_master(self._call("master"))

    def slave_states(self) -> list[dict]:
        return parse_slaves(self._call("slaves"))

    def read_sdo(self, position: int, index: int, subindex: int = 0) -> int:
        """SDO upload of a whitelisted object (post-stop error codes only)."""
        kind = SDO_READ_ALLOWED.get(index)
        if kind is None or subindex != 0:
            raise ValueError(f"refusing SDO read of 0x{index:04X}:{subindex}")
        text = self.runner([self.binary, "upload", "-m", str(self.master), "-p", str(position), "-t", kind, f"0x{index:04X}", str(subindex)])
        token = text.split()[0] if text.split() else ""
        return int(token, 16) if token.lower().startswith("0x") else int(token)


class CounterTracker:
    """Per-interval deltas of master counters.

    `Lost frames` is tx - rx taken from a non-atomic snapshot, so it dips by one while a frame is in
    flight. Deltas are therefore taken against each counter's high-water mark (dips count as 0), and
    only a decrease of `tx_frames` is treated as a master reload (high-water marks reset).
    """

    def __init__(self):
        self.high: Optional[dict] = None

    def update(self, counters: dict) -> dict:
        if self.high is None:
            self.high = dict(counters)
            return {}
        if counters.get("tx_frames", 0) < self.high.get("tx_frames", 0):
            self.high = {key: 0 for key in counters}
        delta = {}
        for key, value in counters.items():
            mark = self.high.get(key)
            if mark is None:
                self.high[key] = value
                continue
            delta[key] = max(0, value - mark)
            self.high[key] = max(mark, value)
        return delta
