"""Read-only LAN web view: run index and per-run reports. GET/HEAD only, no writes, no token."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse

from .archive import INDEX, RUN_FILE, Archive, read_json
from . import anomalies, live
from .compare import render_compare
from .export import mcap_to_csv
from .waveform import waveform
from .experiment_report import render_experiment, render_experiment_compare
from .waveform_view import render_blackbox
from .recorder import load_run
from .report import render

LOG = logging.getLogger("runtime_log.web")
WAVEFORM_LOCK = threading.BoundedSemaphore(1)
EXPERIMENT_FILES = {"data.mcap", "data.csv", "meta.json", "plot.json", "waveform.json"}
BLACKBOX_FILES = {"window.mcap", "window.csv", "trigger.json", "kernel.log", "timeline.json", "waveform.json"}
RUN_FILES = {"report.html", "run.json", "events.jsonl", "resources.jsonl", "ethercat.jsonl", "launch.log.gz"}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def station_header(station: Optional[dict]) -> tuple[str, str]:
    """(title suffix, links to the stations of the other IPCs) from the host overlay."""
    station = station or {}
    name = f" · {esc(station['name'])}" if station.get("name") else ""
    peers = [p for p in station.get("peers") or [] if p.get("url", "").startswith(("http://", "https://"))]
    links = " ".join(f'<a href="{esc(p["url"])}">{esc(p.get("name") or p["url"])} →</a>' for p in peers)
    return name, (f'<p class="k">其他工控机：{links}</p>' if links else "")


def render_index(index: dict, station: Optional[dict] = None) -> str:
    from .index_view import render_home
    return render_home(index, station)


class Handler(BaseHTTPRequestHandler):
    archive: Archive
    enums: dict = {}
    station: dict = {}
    server_version = "Runtime-LOG"

    def log_message(self, fmt, *args):  # keep journald quiet
        LOG.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: bytes, content_type: str, head: bool) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_csv(self, source: Path, filename: str, head: bool) -> None:
        if not source.is_file():
            self._send(404, b"not found", "text/plain", head)
            return
        body = mcap_to_csv(source)
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _send_waveform(self, source: Path, head: bool) -> None:
        if not source.is_file():
            self._send(404, "数据尚未归档或已被清理".encode(), "text/plain; charset=utf-8", head)
            return
        if not WAVEFORM_LOCK.acquire(blocking=False):
            self._send(503, "正在读取其他波形，请稍后重试".encode(), "text/plain; charset=utf-8", head)
            return
        try:
            query = parse_qs(urlparse(self.path).query)
            end = query.get('end', [None])[0]
            origin = query.get('origin', [None])[0]
            data = waveform(source, query.get('joint', [''])[0], float(query.get('start', [0])[0]),
                            float(end) if end is not None else None, int(query.get('width', [1000])[0]),
                            time_origin=float(origin) if origin is not None else None)
            self._send(200, json.dumps(data, allow_nan=False).encode(), 'application/json', head)
        except ValueError as exc:
            self._send(400, str(exc).encode(), 'text/plain; charset=utf-8', head)
        finally:
            WAVEFORM_LOCK.release()

    def _route(self, head: bool) -> None:
        path = unquote(urlparse(self.path).path)
        parts = [part for part in path.split("/") if part]
        root = self.archive.root
        if not parts:
            index = read_json(root / INDEX, {"runs": []})
            self._send(200, render_index(index, self.station).encode(), "text/html; charset=utf-8", head)
            return
        if parts == ["compare"]:
            query = parse_qs(urlparse(self.path).query)
            ids = [query.get(key, [""])[0] for key in ("a", "b")]
            dirs = [root / run_id for run_id in ids]
            if not all(run_id and "/" not in run_id and not run_id.startswith(".") and (d / RUN_FILE).is_file() for run_id, d in zip(ids, dirs)):
                self._send(404, "选择两次已存在的运行".encode(), "text/plain; charset=utf-8", head)
                return
            (run_a, events_a), (run_b, events_b) = load_run(dirs[0]), load_run(dirs[1])
            self._send(200, render_compare(run_a, events_a, run_b, events_b).encode(), "text/html; charset=utf-8", head)
            return
        if parts == ["live"]:
            self._send(200, live.page().encode(), "text/html; charset=utf-8", head)
            return
        if parts == ["live.json"]:
            body = json.dumps(live.snapshot(self.archive, self.enums), ensure_ascii=False).encode()
            self._send(200, body, "application/json", head)
            return
        if parts == ["anomalies.json"]:
            try:
                since = anomalies.parse_since(parse_qs(urlparse(self.path).query).get("since", [""])[0])
            except ValueError as exc:
                self._send(400, str(exc).encode(), "text/plain; charset=utf-8", head)
                return
            body = json.dumps(anomalies.snapshot(self.archive, since), ensure_ascii=False, allow_nan=False).encode()
            self._send(200, body, "application/json", head)
            return
        if parts == ["compare_experiments"]:
            query = parse_qs(urlparse(self.path).query)
            picked = []
            for key in ("a", "b"):
                value = query.get(key, [""])[0]
                run_name, _, exp_name = value.partition("/")
                exp_dir = root / run_name / "experiments" / exp_name
                if not run_name or not exp_name or "/" in exp_name or run_name.startswith(".") or exp_name.startswith(".") \
                        or not (exp_dir / "meta.json").is_file():
                    self._send(404, "选择两个已存在的实验（运行ID/实验目录）".encode(), "text/plain; charset=utf-8", head)
                    return
                picked.append((value, read_json(exp_dir / "meta.json", {})))
            self._send(200, render_experiment_compare(picked[0], picked[1]).encode(), "text/html; charset=utf-8", head)
            return
        if parts == ["index.json"]:
            self._send(200, (root / INDEX).read_bytes() if (root / INDEX).exists() else b"{}", "application/json", head)
            return
        run_id = parts[0]
        run_dir = root / run_id
        if "/" in run_id or run_id.startswith(".") or not (run_dir / RUN_FILE).is_file():
            self._send(404, b"not found", "text/plain", head)
            return
        if len(parts) == 1:
            if not path.endswith("/"):
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", f"/{run_id}/")
                self.end_headers()
                return
            parts.append("report.html")
        if len(parts) in (3, 4) and parts[1] == "experiments" and not parts[2].startswith(".") and "/" not in parts[2]:
            exp_dir = run_dir / "experiments" / parts[2]
            name = parts[3] if len(parts) == 4 else ""
            if not (exp_dir / "meta.json").is_file() or (name and name not in EXPERIMENT_FILES):
                self._send(404, b"not found", "text/plain", head)
                return
            if len(parts) == 3 and not path.endswith("/"):
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", f"/{run_id}/experiments/{parts[2]}/")
                self.end_headers()
                return
            if not name:
                meta = read_json(exp_dir / "meta.json", {})
                plot = read_json(exp_dir / "plot.json", {})
                self._send(200, render_experiment(run_id, meta, plot).encode(), "text/html; charset=utf-8", head)
                return
            if name == "waveform.json":
                self._send_waveform(exp_dir / 'data.mcap', head)
                return
            if name == "data.csv":
                self._send_csv(exp_dir / "data.mcap", f"{run_id}-{parts[2]}.csv", head)
                return
            kinds = {"data.mcap": "application/octet-stream", "meta.json": "application/json", "plot.json": "application/json"}
            self._send(200, (exp_dir / name).read_bytes(), kinds[name], head)
            return
        if len(parts) in (3, 4) and parts[1] == 'blackbox' and not parts[2].startswith('.') and '/' not in parts[2]:
            box_dir = run_dir / 'blackbox' / parts[2]
            name = parts[3] if len(parts) == 4 else ''
            if not (box_dir / 'trigger.json').is_file() or (name and name not in BLACKBOX_FILES):
                self._send(404, b'not found', 'text/plain', head)
                return
            if not name:
                if not path.endswith('/'):
                    self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                    self.send_header('Location', f'/{run_id}/blackbox/{parts[2]}/')
                    self.end_headers()
                    return
                self._send(200, render_blackbox(run_id, parts[2], read_json(box_dir / 'trigger.json', {})).encode(),
                           'text/html; charset=utf-8', head)
                return
            if name == 'waveform.json':
                self._send_waveform(box_dir / 'window.mcap', head)
                return
            if parts[3] == "window.csv":
                self._send_csv(run_dir / "blackbox" / parts[2] / "window.mcap", f"{run_id}-{parts[2]}.csv", head)
                return
            target = run_dir / "blackbox" / parts[2] / parts[3]
            if not target.is_file():
                self._send(404, b"not found", "text/plain", head)
                return
            kinds = {"window.mcap": "application/octet-stream", "trigger.json": "application/json", "kernel.log": "text/plain; charset=utf-8", "timeline.json": "application/json"}
            self._send(200, target.read_bytes(), kinds[parts[3]], head)
            return
        name = parts[1]
        if len(parts) != 2 or name not in RUN_FILES:
            self._send(404, b"not found", "text/plain", head)
            return
        if name == "report.html":
            document, events = load_run(run_dir)
            # Render archived runs with the current UI too; do not rewrite their evidence.
            body = render(document, events, run_dir).encode()
            self._send(200, body, "text/html; charset=utf-8", head)
            return
        target = run_dir / name
        if not target.is_file():
            self._send(404, b"not found", "text/plain", head)
            return
        content_type = "application/gzip" if name.endswith(".gz") else (mimetypes.guess_type(name)[0] or "text/plain; charset=utf-8")
        if name.endswith(".jsonl"):
            content_type = "text/plain; charset=utf-8"
        self._send(200, target.read_bytes(), content_type, head)

    def do_GET(self):  # noqa: N802
        try:
            self._route(False)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("request failed: %s", self.path)
            self._send(500, b"internal error", "text/plain", False)

    def do_HEAD(self):  # noqa: N802
        self._route(True)

    def do_POST(self):  # noqa: N802
        path = unquote(urlparse(self.path).path)
        if path != "/live/zero":
            self._refuse()
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send(403, json.dumps({"error": "跨站请求已拒绝"}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", False)
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, json.dumps({"error": "需要 application/json"}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", False)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("invalid content length")
            request = json.loads(self.rfile.read(length))
            sensor = request.get("sensor")
            action = request.get("action")
            if sensor not in live.FORCE_SENSORS or action not in ("capture", "disable"):
                raise ValueError("invalid sensor or action")
            if action == "capture":
                row = live.current_force_sensor(live.snapshot(self.archive, self.enums), sensor)
                if row is None or any(value is None for value in row.get("raw", [])):
                    self._send(409, json.dumps({"error": "该臂当前没有新鲜、完整的六轴样本，未修改零点"}, ensure_ascii=False).encode(),
                               "application/json; charset=utf-8", False)
                    return
                live.save_force_zero(self.archive, sensor, row["raw"], "live-panel-current-sample")
                message = f"{live.FORCE_SENSORS[sensor]}软件零点已更新"
            else:
                live.save_force_zero(self.archive, sensor, None, "live-panel-disable")
                message = f"{live.FORCE_SENSORS[sensor]}已恢复显示未清零值"
            self._send(200, json.dumps({"ok": True, "message": message}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", False)
        except (ValueError, TypeError, json.JSONDecodeError):
            self._send(400, json.dumps({"error": "请求格式无效"}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", False)


    def _refuse(self):
        self._send(405, b"read-only", "text/plain", False)

    do_PUT = do_DELETE = do_PATCH = _refuse


def serve(archive: Archive, host: str, port: int, enums: Optional[dict] = None,
          station: Optional[dict] = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"archive": archive, "enums": enums or {}, "station": station or {}})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="runtime-log-web", daemon=True)
    thread.start()
    LOG.info("web on http://%s:%s/", host, port)
    return server


__all__ = ["serve", "render_index", "json"]
