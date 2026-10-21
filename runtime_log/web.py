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
from . import live
from .compare import render_compare
from .export import mcap_to_csv
from .waveform import waveform
from .experiment_report import render_experiment, render_experiment_compare
from .recorder import load_run
from .report import CSS, RESULT_COLOR, RESULT_LABEL, fmt_s, fmt_time, render

LOG = logging.getLogger("runtime_log.web")
WAVEFORM_LOCK = threading.BoundedSemaphore(1)
EXPERIMENT_FILES = {"data.mcap", "data.csv", "meta.json", "plot.json", "waveform.json"}
BLACKBOX_FILES = {"window.mcap", "window.csv", "trigger.json", "kernel.log", "timeline.json"}
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
    title, peers = station_header(station)
    runs = index.get("runs", [])
    usage = index.get("usage", {})
    rows = []
    for run in runs:
        result = run.get("result") or "?"
        color = RESULT_COLOR.get(result, "#57606a")
        anomalies = {k: v for k, v in (run.get("anomalies") or {}).items() if k.startswith(("ecat.", "process.died", "log.failures", "log.error"))}
        top = ", ".join(f"{k.split('.', 1)[1]}×{v}" for k, v in sorted(anomalies.items(), key=lambda kv: -kv[1])[:3])
        detail = run.get("failure_label") or ""
        if run.get("failed_stage"):
            detail = f'<b>{esc(run["failed_stage"])}</b>：{esc(detail)}'
        else:
            detail = esc(detail)
        rows.append(
            f'<tr data-result="{esc(result)}" data-hw="{esc(run.get("hardware"))}">'
            f'<td><input type="checkbox" class="pick" value="{esc(run["id"])}"></td>'
            f'<td class="mono">{esc(fmt_time(run.get("start")))}</td>'
            f'<td><a href="{esc(run["id"])}/">{esc(run["id"])}</a></td>'
            f'<td>{esc(run.get("scope"))}</td><td class="num">{esc(run.get("domain"))}</td><td>{esc(run.get("hardware"))}</td>'
            f'<td><span class="badge" style="background:{color}">{esc(RESULT_LABEL.get(result, result))}</span></td>'
            f'<td class="num">{fmt_s(run.get("duration_s"))}</td><td class="num">{fmt_s(run.get("ready_s"))}</td>'
            f'<td>{detail}</td><td>{esc(run.get("last_stage"))}</td>'
            f'<td class="num">{sum(anomalies.values()) or ""}</td><td class="mono">{esc(top)}</td>'
            f'<td class="num">{esc(run.get("peak_cpu") if run.get("peak_cpu") is not None else "")}</td>'
            f'<td class="num">{run.get("blackbox_saved") or ""}</td><td class="num">{run.get("experiments") or ""}</td><td>{esc(run.get("source"))}</td></tr>'
        )
    running = any(run.get("result") == "running" for run in runs)
    refresh = '<meta http-equiv="refresh" content="10">' if running else ""
    archive_mb = (usage.get("archive_bytes") or 0) / 1024**2
    free_gb = (usage.get("disk_free_bytes") or 0) / 1024**3
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{refresh}<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Runtime-LOG</title><style>{CSS}</style></head><body><main style="max-width:1600px">
<h1>Runtime-LOG · 运行记录{title} <a href="/live" style="font-size:14px">实时状态 →</a></h1>{peers}
<p class="k">共 {len(runs)} 次运行 · 归档 {archive_mb:.1f} MiB · 磁盘剩余 {free_gb:.1f} GiB · 保留 7 天 · 只读页面 · 更新于 {fmt_time(index.get("generated"))}</p>
<div class="filters"><label>结果 <select id="fResult"><option value="">全部</option><option value="failed">失败</option><option value="ok">正常结束</option><option value="running">运行中</option><option value="aborted">未正常结束</option><option value="interrupted">记录中断</option></select></label>
<label>硬件 <select id="fHw"><option value="">全部</option><option>ethercat</option><option>mock</option></select></label>
<label><input id="fAnomaly" type="checkbox"> 只看有异常</label><label>搜索 <input id="fText" size="24"></label></div>
<p><button id="compareBtn" disabled>对比所选两次运行</button> <span class="k">勾选两行后对比（先选的为 A）</span></p>
<table id="runs"><tr><th></th><th>开始</th><th>运行</th><th>scope</th><th>域</th><th>硬件</th><th>结果</th><th>时长</th><th>就绪</th><th>失败步骤</th><th>最后成功步骤</th><th>异常</th><th>主要异常</th><th>峰值 CPU%</th><th>黑匣子</th><th>实验</th><th>来源</th></tr>
{"".join(rows) or '<tr><td colspan="17">还没有运行记录</td></tr>'}</table></main>
<script>
const rows=[...document.querySelectorAll('#runs tr[data-result]')];
function apply(){{const r=fResult.value,h=fHw.value,a=fAnomaly.checked,t=fText.value.toLowerCase();
rows.forEach(x=>{{const ok=(!r||x.dataset.result===r)&&(!h||x.dataset.hw===h)&&(!a||x.cells[11].textContent.trim())&&(!t||x.textContent.toLowerCase().includes(t));x.style.display=ok?'':'none'}})}}
[fResult,fHw,fAnomaly,fText].forEach(e=>e.oninput=e.onchange=apply);
const picked=[];document.querySelectorAll('.pick').forEach(c=>c.onchange=()=>{{const i=picked.indexOf(c.value);if(c.checked){{picked.push(c.value);while(picked.length>2){{const old=picked.shift();document.querySelector('.pick[value="'+old+'"]').checked=false}}}}else if(i>=0)picked.splice(i,1);compareBtn.disabled=picked.length!==2}});
compareBtn.onclick=()=>{{location.href='/compare?a='+encodeURIComponent(picked[0])+'&b='+encodeURIComponent(picked[1])}};
</script></body></html>"""


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
                source = exp_dir / "data.mcap"
                if not source.is_file():
                    self._send(404, "实验数据尚未归档或已被清理".encode(), "text/plain; charset=utf-8", head)
                    return
                if not WAVEFORM_LOCK.acquire(blocking=False):
                    self._send(503, "正在读取其他波形，请稍后重试".encode(), "text/plain; charset=utf-8", head)
                    return
                try:
                    query = parse_qs(urlparse(self.path).query)
                    end = query.get("end", [None])[0]
                    data = waveform(source, query.get("joint", [""])[0],
                                    float(query.get("start", [0])[0]),
                                    float(end) if end is not None else None,
                                    int(query.get("width", [1000])[0]))
                    self._send(200, json.dumps(data, allow_nan=False).encode(), "application/json", head)
                except ValueError as exc:
                    self._send(400, str(exc).encode(), "text/plain; charset=utf-8", head)
                finally:
                    WAVEFORM_LOCK.release()
                return
            if name == "data.csv":
                self._send_csv(exp_dir / "data.mcap", f"{run_id}-{parts[2]}.csv", head)
                return
            kinds = {"data.mcap": "application/octet-stream", "meta.json": "application/json", "plot.json": "application/json"}
            self._send(200, (exp_dir / name).read_bytes(), kinds[name], head)
            return
        if len(parts) == 4 and parts[1] == "blackbox" and parts[3] in BLACKBOX_FILES and not parts[2].startswith(".") and "/" not in parts[2]:
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
            stored = run_dir / "report.html"
            if document.get("result") != "running" and stored.exists():
                body = stored.read_bytes()
            else:
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

    def _refuse(self):
        self._send(405, b"read-only", "text/plain", False)

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse


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
