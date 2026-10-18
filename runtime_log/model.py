"""Common event record shared by every source."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Event kinds
PROCESS_STARTED = "process_started"
PROCESS_EXITED = "process_exited"
STAGE = "stage"
FAILURE = "failure"
SHUTDOWN = "shutdown"
CONTROL = "control"
ERROR = "error"
WARNING = "warning"
ECAT = "ecat"
SIGNAL = "signal"

LEVELS = ("info", "warn", "error")


@dataclass
class Event:
    ts: float  # epoch seconds
    kind: str
    source: str  # launch | kernel | ethercat | proc | runtime-log
    subject: str = ""
    message: str = ""
    level: str = "info"
    rule: str = ""  # profile/classifier rule id
    label: str = ""  # human-readable label from the rule
    data: dict[str, Any] = field(default_factory=dict)
    mono: Optional[float] = None  # CLOCK_MONOTONIC seconds when known

    def to_dict(self, t0: Optional[float] = None) -> dict[str, Any]:
        record = asdict(self)
        if t0 is not None:
            record["t_rel_s"] = round(self.ts - t0, 6)
        return record

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> "Event":
        fields = {k: record[k] for k in cls.__dataclass_fields__ if k in record}
        return cls(**fields)
