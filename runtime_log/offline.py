"""Offline paths: import a past ROS launch log directory, and replay an archived run."""

from __future__ import annotations

import gzip
import json
import re
import shutil
from pathlib import Path
from typing import Iterable, Optional

from . import kernlog, timeline
from .archive import RUN_FILE, Archive, read_json, run_id, write_json
from .launchlog import read_lines
from .meta import artifact_info, hardware_kind, host_info
from .model import Event
from .procfs import ProcFS
from .profile import Profile
from .recorder import counter_events, load_run
from .report import read_jsonl, write_report
from .sources import kernel_events, launch_events


def _events_file(run_dir: Path, events: list[Event], t0: float) -> None:
    temporary = run_dir / ".events.jsonl.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        for event in timeline.sort_events(events):
            stream.write(json.dumps(event.to_dict(t0), ensure_ascii=False, separators=(",", ":")) + "\n")
    temporary.replace(run_dir / "events.jsonl")


PATH_TOKEN = re.compile(r"/[\w.@+-]+(?:/[\w.@+-]+)+")


def find_artifact_dir(lines: Iterable[str]) -> Optional[Path]:
    """First existing deployment artifact directory mentioned in the log (contains artifact.yaml)."""
    seen = set()
    for line in lines:
        for token in PATH_TOKEN.findall(line):
            path = Path(token)
            for candidate in (path, *list(path.parents)[:3]):
                if candidate in seen or str(candidate) in ("/", "/home", "/tmp"):
                    continue
                seen.add(candidate)
                if (candidate / "artifact.yaml").is_file():
                    return candidate
    return None


def import_launch_dir(
    log_dir: Path,
    profile: Profile,
    archive: Archive,
    scope: str = "",
    domain: str = "?",
    hardware: str = "",
    kernel_lines: Optional[Iterable[str]] = None,
) -> dict:
    """Build an archived run from a finished ROS launch directory (no resource samples)."""
    launch_log = log_dir / "launch.log"
    text = launch_log.read_text(encoding="utf-8", errors="replace").splitlines()
    events = launch_events(text, profile)
    if not events:
        raise ValueError(f"{launch_log} has no parsable lines")
    start = min(e.ts for e in events)
    end = max(e.ts for e in events)
    if kernel_lines is None:
        from .service import kernel_history

        kernel = kernel_history(start - 1.0, end + 3.0)
    else:
        kernel = kernel_events(kernel_lines, start - 1.0, end + 3.0)
    events += kernel
    summary = timeline.summarize(events, profile.ready_stages)
    artifact: dict = {}
    artifact_dir = find_artifact_dir(text)
    if artifact_dir is not None:
        artifact = artifact_info(artifact_dir)
        artifact["note"] = "导入时读取的是产物目录的当前内容，可能晚于这次运行"
        selection = artifact.get("selection") or {}
        scope = scope or selection.get("control_scope", "")
        if not hardware:
            mock, uses_ethercat = hardware_kind(artifact, profile.run, {})
            hardware = "mock" if mock else ("ethercat" if uses_ethercat else "other")
    identifier = archive.find_run(ros_log_dir=str(log_dir)) or archive.unique_id(run_id(start, domain, scope or "import"))
    run_dir = archive.run_dir(identifier)
    pid = log_dir.name.rsplit("-", 1)[-1]
    document = {
        "id": identifier,
        "source": "import",
        "start": summary["t0"],
        "end": summary["end"],
        "domain": domain,
        "scope": scope,
        "launch_pid": int(pid) if pid.isdigit() else None,
        "ros_log_dir": str(log_dir),
        "hardware": hardware or None,
        "mock": None if not hardware else hardware == "mock",
        "host": host_info(ProcFS()),
        "profile": profile.name,
        "result": summary["result"],
        "summary": summary,
        "resources": {},
        "artifact": artifact,
        "note": "离线导入：只有日志与内核事件，没有资源与主站轮询数据。",
    }
    _events_file(run_dir, events, summary["t0"])
    with launch_log.open("rb") as source, gzip.open(run_dir / "launch.log.gz", "wb") as target:
        shutil.copyfileobj(source, target)
    write_json(run_dir / RUN_FILE, document)
    write_report(run_dir, document, timeline.sort_events(events))
    archive.update_index()
    return document


def replay(run_dir: Path, profile: Profile) -> tuple[dict, list[Event]]:
    """Re-classify the archived launch.log with the current profile; keep kernel/polling events."""
    document, stored = load_run(run_dir)
    if not document:
        raise FileNotFoundError(f"{run_dir / RUN_FILE} missing")
    archived_log = run_dir / "launch.log.gz"
    if archived_log.exists():
        with gzip.open(archived_log, "rt", encoding="utf-8", errors="replace") as stream:
            events = []
            for line in read_lines(stream):
                events.extend(profile.classify(line))
        for event in stored:
            if event.source == "kernel":
                event = kernlog.reclassify(event)
            if event is None or event.source == "launch" or event.rule == "counter_increase":
                continue
            events.append(event)
        events += counter_events(read_jsonl(run_dir / "ethercat.jsonl"))
    else:
        events = stored
    running = document.get("result") == "running"
    summary = timeline.summarize(events, profile.ready_stages, running=running)
    if document.get("result") == "interrupted" and summary["result"] in ("aborted", "running"):
        summary["result"] = "interrupted"
    document["summary"] = summary
    document["result"] = summary["result"]
    if not running:
        _events_file(run_dir, events, summary["t0"])
        write_json(run_dir / RUN_FILE, document)
        write_report(run_dir, document, timeline.sort_events(events))
    return document, timeline.sort_events(events)


def text_summary(document: dict) -> str:
    summary = document.get("summary") or {}
    lines = [f"{document.get('id')}  结果={document.get('result')}  时长={summary.get('duration_s')} s  就绪={summary.get('ready_s')} s"]
    failure = summary.get("failure")
    if failure:
        lines.append(f"  失败步骤: {summary.get('failed_stage')}  @{failure.get('t_rel_s')} s  {failure.get('label')}")
        lines.append(f"    {failure.get('message', '')[:200]}")
    last = summary.get("last_stage") or {}
    lines.append(f"  最后成功步骤: {last.get('label', '无')} @{last.get('t_rel_s', '-')} s")
    for key, value in (summary.get("anomalies") or {}).items():
        lines.append(f"  异常 {key}: {value}")
    return "\n".join(lines)


def resolve_run(archive: Archive, name: str) -> Path:
    path = Path(name).expanduser()
    if (path / RUN_FILE).exists():
        return path
    candidate = archive.root / name
    if (candidate / RUN_FILE).exists():
        return candidate
    matches = [p for p in archive.root.iterdir() if p.name.startswith(name) and (p / RUN_FILE).exists()]
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(f"no unique run matches {name!r}")


__all__ = ["import_launch_dir", "replay", "text_summary", "resolve_run", "read_json"]
