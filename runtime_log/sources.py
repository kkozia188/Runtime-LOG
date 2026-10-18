"""Whole-file event extraction (used by `import` and by the live recorder's incremental reads)."""

from __future__ import annotations

from typing import Iterable

from . import kernlog
from .launchlog import read_lines
from .model import Event
from .profile import Profile


def launch_events(lines: Iterable[str], profile: Profile) -> list[Event]:
    events: list[Event] = []
    for line in read_lines(lines):
        events.extend(profile.classify(line))
    return events


def kernel_events(lines: Iterable[str], start: float | None = None, end: float | None = None) -> list[Event]:
    events = []
    for event in kernlog.parse_lines(lines):
        if start is not None and event.ts < start:
            continue
        if end is not None and event.ts > end:
            continue
        events.append(event)
    return events
