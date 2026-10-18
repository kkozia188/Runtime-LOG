"""Archive layout, atomic writes, run index, 7-day retention and disk-space guard."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

INDEX = "index.json"
RUN_FILE = "run.json"


def write_json(path: Path, document: Any) -> None:
    """Write via a temporary file in the same directory and rename (readers never see half a file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def run_id(start: float, domain: str, scope: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
    def safe(text: str) -> str:
        return "".join(c if c.isascii() and (c.isalnum() or c in "_-") else "_" for c in text)

    return f"{stamp}-d{safe(str(domain)) if str(domain).isdigit() else 'x'}-{safe(scope or 'unknown')}"


class Archive:
    def __init__(self, root: str | Path, retention_days: float = 7.0, min_free_gb: float = 10.0):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.retention_s = retention_days * 86400.0
        self.min_free_bytes = int(min_free_gb * 1024**3)

    def run_dir(self, identifier: str) -> Path:
        path = self.root / identifier
        path.mkdir(parents=True, exist_ok=True)
        return path

    def unique_id(self, identifier: str) -> str:
        candidate, number = identifier, 2
        while (self.root / candidate).exists():
            candidate = f"{identifier}-{number}"
            number += 1
        return candidate

    def find_run(self, **match) -> Optional[str]:
        for run in self.runs():
            if all(run.get(key) == value for key, value in match.items()):
                return run["id"]
        return None

    def runs(self) -> list[dict]:
        rows = []
        for entry in self.root.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                document = read_json(entry / RUN_FILE)
                if document:
                    rows.append(document)
        return sorted(rows, key=lambda r: r.get("start", 0), reverse=True)

    def update_index(self) -> list[dict]:
        rows = [_index_row(run) for run in self.runs()]
        write_json(self.root / INDEX, {"generated": time.time(), "runs": rows, "usage": self.usage()})
        return rows

    def usage(self) -> dict:
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
        stat = shutil.disk_usage(self.root)
        return {"archive_bytes": total, "disk_free_bytes": stat.free, "disk_total_bytes": stat.total}

    def collect_garbage(self, now: Optional[float] = None, active: Iterable[str] = ()) -> list[str]:
        """Delete runs older than the retention period, then oldest runs while disk space is low."""
        now = now if now is not None else time.time()
        active = set(active)
        removed = []
        runs = sorted(self.runs(), key=lambda r: r.get("end") or r.get("start") or 0)
        for run in runs:
            if run["id"] in active or run.get("result") == "running":
                continue
            if now - (run.get("end") or run.get("start") or now) > self.retention_s:
                removed.append(self._remove(run["id"]))
        for run in runs:
            if shutil.disk_usage(self.root).free >= self.min_free_bytes:
                break
            if run["id"] in active or run["id"] in removed or run.get("result") == "running":
                continue
            removed.append(self._remove(run["id"]))
        if removed:
            self.update_index()
        return removed

    def low_disk(self) -> bool:
        return shutil.disk_usage(self.root).free < self.min_free_bytes

    def _remove(self, identifier: str) -> str:
        target = self.root / identifier
        if target.parent == self.root and target.is_dir():
            shutil.rmtree(target)
        return identifier


def _index_row(run: dict) -> dict:
    summary = run.get("summary", {})
    last = summary.get("last_stage") or {}
    failure = summary.get("failure") or {}
    return {
        "id": run["id"],
        "start": run.get("start"),
        "end": run.get("end"),
        "duration_s": summary.get("duration_s"),
        "domain": run.get("domain"),
        "scope": run.get("scope"),
        "mock": run.get("mock"),
        "hardware": run.get("hardware"),
        "result": run.get("result"),
        "failed_stage": summary.get("failed_stage", ""),
        "failure_label": failure.get("label") or failure.get("message", "")[:120],
        "last_stage": last.get("label", ""),
        "ready_s": summary.get("ready_s"),
        "anomaly_total": summary.get("anomaly_total", 0),
        "anomalies": summary.get("anomalies", {}),
        "peak_cpu": run.get("resources", {}).get("peak_cpu_percent"),
        "source": run.get("source", "live"),
    }
