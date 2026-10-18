"""Parse ROS 2 launch `launch.log` lines (Humble format) into events.

Two line shapes exist:
  <epoch> [LEVEL] [<launch-name>]: <message>                 launch's own messages
  <epoch> [<process>] [LEVEL] [<ros-time>] [<logger>]: <msg> node output (ROS logger)
  <epoch> [<process>] <raw text>                            node output (print/traceback)
"""

from __future__ import annotations

import re
from typing import Iterable, Iterator, Optional

from .model import ERROR, PROCESS_EXITED, PROCESS_STARTED, SIGNAL, WARNING, Event

ANSI = re.compile(r"\x1b\[[0-9;]*m")
LAUNCH_LINE = re.compile(r"^(?P<ts>\d+\.\d+) \[(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\] \[(?P<name>[^\]]+)\]: (?P<msg>.*)$")
NODE_LOGGER = re.compile(
    r"^(?P<ts>\d+\.\d+) \[(?P<proc>[^\]]+)\] \[(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\] "
    r"\[(?P<rts>\d+\.\d+)\] \[(?P<logger>[^\]]+)\]: (?P<msg>.*)$"
)
NODE_RAW = re.compile(r"^(?P<ts>\d+\.\d+) \[(?P<proc>[^\]]+)\] (?P<msg>.*)$")

STARTED = re.compile(r"process started with pid \[(?P<pid>\d+)\]")
FINISHED = re.compile(r"process has finished cleanly \[pid (?P<pid>\d+)\]")
DIED = re.compile(r"process has died \[pid (?P<pid>\d+), exit code (?P<code>-?\d+)(?:, cmd '(?P<cmd>.*?)')?")
SENDING_SIGNAL = re.compile(r"sending signal '(?P<sig>\w+)' to process\[(?P<target>[^\]]+)\]")

_LEVEL = {"DEBUG": "info", "INFO": "info", "WARN": "warn", "WARNING": "warn", "ERROR": "error", "FATAL": "error"}


def base_name(process: str) -> str:
    """`ros2_control_node-1` -> `ros2_control_node`."""
    return re.sub(r"-\d+$", "", process)


class LogLine:
    """One parsed line, before profile classification."""

    __slots__ = ("ts", "process", "logger", "level", "message", "launch")

    def __init__(self, ts, process, logger, level, message, launch):
        self.ts = ts
        self.process = process  # launch process instance name, e.g. ros2_control_node-1
        self.logger = logger  # ROS logger name or "" for raw output
        self.level = level  # info|warn|error|"" (raw)
        self.message = message
        self.launch = launch  # True for launch's own lines

    @property
    def text(self) -> str:
        """Text used for rule matching: `[logger]: message` keeps the logger visible."""
        return f"[{self.logger}]: {self.message}" if self.logger else self.message


def parse_line(line: str) -> Optional[LogLine]:
    line = ANSI.sub("", line.rstrip("\n"))
    match = NODE_LOGGER.match(line)
    if match:
        return LogLine(float(match["ts"]), match["proc"], match["logger"], _LEVEL[match["level"]], match["msg"], False)
    match = LAUNCH_LINE.match(line)
    if match:
        return LogLine(float(match["ts"]), match["name"], "", _LEVEL[match["level"]], match["msg"], True)
    match = NODE_RAW.match(line)
    if match:
        return LogLine(float(match["ts"]), match["proc"], "", "", match["msg"], False)
    return None


def lifecycle_event(line: LogLine) -> Optional[Event]:
    """Process start/exit and launch signals; these need no profile."""
    if not line.launch:
        return None
    name = line.process
    match = STARTED.search(line.message)
    if match:
        return Event(line.ts, PROCESS_STARTED, "launch", name, line.message, "info", data={"pid": int(match["pid"]), "base": base_name(name)})
    match = FINISHED.search(line.message)
    if match:
        return Event(line.ts, PROCESS_EXITED, "launch", name, line.message, "info", data={"pid": int(match["pid"]), "exit_code": 0, "base": base_name(name)})
    match = DIED.search(line.message)
    if match:
        code = int(match["code"])
        return Event(line.ts, PROCESS_EXITED, "launch", name, line.message, "error" if code else "info", data={"pid": int(match["pid"]), "exit_code": code, "base": base_name(name)})
    match = SENDING_SIGNAL.search(line.message)
    if match:
        return Event(line.ts, SIGNAL, "launch", match["target"], line.message, "info", data={"signal": match["sig"]})
    return None


def level_event(line: LogLine) -> Optional[Event]:
    """Every ERROR/FATAL and WARN line (rule-matched lines are reported by the classifier instead)."""
    if line.level == "error":
        return Event(line.ts, ERROR, "launch", line.process, line.text, "error")
    if line.level == "warn":
        return Event(line.ts, WARNING, "launch", line.process, line.text, "warn")
    return None


def read_lines(lines: Iterable[str]) -> Iterator[LogLine]:
    for raw in lines:
        parsed = parse_line(raw)
        if parsed is not None:
            yield parsed
