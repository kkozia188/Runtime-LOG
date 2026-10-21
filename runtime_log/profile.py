"""Project profile: stage/failure/shutdown/control rules and run attributes, loaded from YAML."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .launchlog import LogLine, level_event, lifecycle_event
from .model import CONTROL, FAILURE, SHUTDOWN, STAGE, Event


@dataclass
class Rule:
    id: str
    kind: str  # stage | failure | shutdown | control
    pattern: re.Pattern
    label: str = ""
    stage: str = ""  # failure rules: which startup step failed
    source: str = "any"  # launch | node | any
    level: str = "info"

    def matches(self, line: LogLine) -> Optional[re.Match]:
        if self.source == "launch" and not line.launch:
            return None
        if self.source == "node" and line.launch:
            return None
        return self.pattern.search(line.text)


@dataclass
class Profile:
    name: str
    rules: list[Rule]
    stage_order: list[str]
    ready_stages: list[str]
    run: dict = field(default_factory=dict)
    ethercat: dict = field(default_factory=dict)
    git_repos: list[str] = field(default_factory=list)
    process_order: list[str] = field(default_factory=list)
    blackbox: dict = field(default_factory=dict)
    # This station (host overlay only): display name and links to the stations of other IPCs.
    station: dict = field(default_factory=dict)

    def classify(self, line: LogLine) -> list[Event]:
        """Events for one launch.log line: process lifecycle, rule hits, and unmatched WARN/ERROR."""
        events = []
        lifecycle = lifecycle_event(line)
        if lifecycle is not None:
            events.append(lifecycle)
        matched = False
        for rule in self.rules:
            match = rule.matches(line)
            if not match:
                continue
            matched = True
            data = {k: v for k, v in match.groupdict().items() if v is not None}
            if rule.stage:
                data["stage"] = rule.stage
            level = "error" if rule.kind == FAILURE else rule.level
            events.append(
                Event(line.ts, rule.kind, "launch", line.process, line.text, level, rule=rule.id, label=rule.label or rule.id, data=data)
            )
            break
        if not matched and lifecycle is None:
            plain = level_event(line)
            if plain is not None:
                events.append(plain)
        return events


_KINDS = {"stages": STAGE, "failures": FAILURE, "shutdown": SHUTDOWN, "controls": CONTROL}


def merge(base, overlay):
    """Overlay wins; mappings merge recursively, every other value (lists included) is replaced."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            merged[key] = merge(base.get(key), value) if key in base else value
        return merged
    return overlay


def load_profile(path: str | Path, host_path: Optional[str | Path] = None) -> Profile:
    """Project profile, optionally overlaid by a per-IPC host file (paths, expected codes, station)."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if host_path is not None and Path(host_path).is_file():
        document = merge(document, yaml.safe_load(Path(host_path).read_text(encoding="utf-8")) or {})
    rules: list[Rule] = []
    # Failures first: a line that reports a failure must not be read as a stage.
    for section in ("failures", "shutdown", "stages", "controls"):
        for item in document.get(section, []) or []:
            rules.append(
                Rule(
                    id=item["id"],
                    kind=_KINDS[section],
                    pattern=re.compile(item["match"]),
                    label=item.get("label", ""),
                    stage=item.get("stage", ""),
                    source=item.get("source", "any"),
                    level=item.get("level", "info"),
                )
            )
    stage_order = [item["id"] for item in document.get("stages", []) or []]
    return Profile(
        name=document.get("name", Path(path).stem),
        rules=rules,
        stage_order=stage_order,
        ready_stages=list(document.get("ready_stages", [])),
        run=document.get("run", {}) or {},
        ethercat=document.get("ethercat", {}) or {},
        git_repos=list(document.get("git_repos", []) or []),
        process_order=list(document.get("process_order", []) or []),
        blackbox=document.get("blackbox", {}) or {},
        station=document.get("station", {}) or {},
    )
