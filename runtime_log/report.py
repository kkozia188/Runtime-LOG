"""Self-contained HTML report for one run (inline SVG, no external resources)."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Optional

from .model import CONTROL, ECAT, ERROR, FAILURE, PROCESS_EXITED, PROCESS_STARTED, SHUTDOWN, SIGNAL, STAGE, WARNING, Event
from .timeline import is_anomaly_key_informational

RESULT_LABEL = {"ok": "正常结束", "failed": "失败", "running": "运行中", "aborted": "未正常结束", "interrupted": "记录中断"}
RESULT_COLOR = {"ok": "#1a7f37", "failed": "#cf222e", "running": "#0969da", "aborted": "#9a6700", "interrupted": "#9a6700"}
STATE_COLOR = {"INIT": "#d0d7de", "PREOP": "#f2cc60", "BOOT": "#bf8700", "SAFEOP": "#54aeff", "OP": "#2da44e", "?": "#ffffff"}
PALETTE = ["#0969da", "#cf222e", "#1a7f37", "#8250df", "#bf8700", "#1b7c83", "#d1242f", "#57606a"]

CSS = """
body{font:14px/1.45 system-ui,-apple-system,"Segoe UI","Noto Sans CJK SC",sans-serif;margin:0;color:#1f2328;background:#f6f8fa}
main{max-width:1280px;margin:0 auto;padding:16px 20px 48px}
h1{font-size:20px;margin:8px 0}h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
.badge{display:inline-block;white-space:nowrap;padding:2px 10px;border-radius:12px;color:#fff;font-weight:600}
.card{background:#fff;border:1px solid #d0d7de;border-radius:6px;padding:12px 16px;margin:8px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:4px 16px}
.k{color:#59636e;font-size:12px}.v{font-weight:600;word-break:break-all}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #d0d7de;padding:3px 6px;text-align:left;vertical-align:top}th{background:#f6f8fa}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.error td{background:#ffebe9}tr.warn td{background:#fff8c5}tr.hit td{background:#ffebe9;font-weight:600}
.fail{border-left:4px solid #cf222e}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
svg{background:#fff;border:1px solid #d0d7de;border-radius:6px;display:block}
svg text{font:11px system-ui,sans-serif;fill:#1f2328}
.legend span{display:inline-block;margin-right:12px;font-size:12px}.legend i{display:inline-block;width:10px;height:10px;margin-right:4px;vertical-align:middle}
details summary{cursor:pointer;color:#0969da}.filters label{margin-right:12px;font-size:13px}
a{color:#0969da}
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def fmt_time(epoch: Optional[float]) -> str:
    if not epoch:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def fmt_s(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.3f} s"


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


class Scale:
    def __init__(self, t_max: float, left: int = 190, width: int = 1220):
        self.t_max = max(t_max, 0.001)
        self.left = left
        self.plot = width - left - 60
        self.width = width

    def x(self, t: float) -> float:
        return self.left + max(0.0, min(t, self.t_max)) / self.t_max * self.plot

    def axis(self, y: int, height: int) -> str:
        step = _nice_step(self.t_max)
        parts = []
        tick = 0.0
        while tick <= self.t_max + 1e-9:
            x = self.x(tick)
            parts.append(f'<line x1="{x:.1f}" y1="{y}" x2="{x:.1f}" y2="{y + height}" stroke="#eaeef2"/>')
            parts.append(f'<text x="{x:.1f}" y="{y + height + 13}" text-anchor="middle">{tick:g}s</text>')
            tick += step
        return "".join(parts)


def _nice_step(span: float) -> float:
    for step in (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1800, 3600):
        if span / step <= 14:
            return float(step)
    return 7200.0


def startup_window(summary: dict) -> Optional[float]:
    """End of the zoomed startup view: ready (or failure) + 3 s; None when the full view is short anyway."""
    failure = summary.get("failure") or {}
    end = summary.get("ready_s") or failure.get("t_rel_s") or (summary.get("last_stage") or {}).get("t_rel_s")
    if end is None:
        return None
    end += 3.0
    return end if (summary.get("duration_s") or 0) > end * 1.5 else None


def gantt(summary: dict, events: list[Event], ecat_rows: list[dict], t0: float, t_max: float) -> str:
    scale = Scale(t_max)
    events = [e for e in events if e.ts - t0 <= t_max]
    ecat_rows = [r for r in ecat_rows if r.get("t", 0) <= t_max]
    rows: list[str] = []
    labels: list[str] = []
    row_h = 20
    y = 8

    def row_label(text: str) -> None:
        labels.append(f'<text x="8" y="{y + 14}">{esc(text[:30])}</text>')

    # phases
    row_label("阶段")
    phase_colors = {"startup": "#ddf4ff", "running": "#dafbe1", "stopping": "#fff8c5"}
    for name, window in (summary.get("phases") or {}).items():
        if window:
            x1, x2 = scale.x(window[0]), scale.x(window[1])
            rows.append(f'<rect x="{x1:.1f}" y="{y + 3}" width="{max(1.0, x2 - x1):.1f}" height="14" fill="{phase_colors[name]}" stroke="#afb8c1"><title>{name} {window[0]:.3f}–{window[1]:.3f} s</title></rect>')
            rows.append(f'<text x="{x1 + 3:.1f}" y="{y + 14}">{ {"startup": "启动", "running": "运行", "stopping": "停止"}[name]}</text>')
    y += row_h

    # processes
    for process in summary.get("processes", []):
        row_label(process["name"])
        x1 = scale.x(process["start"] - t0)
        x2 = scale.x(process["end"] - t0)
        code = process.get("exit_code")
        end_rel = process["end"] - t0
        shutdown = summary.get("shutdown_s")
        failed_exit = code not in (None, 0) and (shutdown is None or end_rel < shutdown)
        color = "#cf222e" if failed_exit else ("#d4a72c" if code not in (None, 0) else "#8c959f")
        title = f'{process["name"]} pid {process.get("pid")} {process["start"] - t0:.3f}–{process["end"] - t0:.3f} s exit={code}'
        rows.append(f'<rect x="{x1:.1f}" y="{y + 4}" width="{max(1.5, x2 - x1):.1f}" height="12" fill="{color}" opacity=".75"><title>{esc(title)}</title></rect>')
        if code not in (None, 0) and end_rel <= t_max and not process.get("end_open"):
            rows.append(f'<text x="{x2 + 3:.1f}" y="{y + 14}" style="fill:{color}">exit {code}</text>')
        y += row_h

    # stage markers
    def markers(title: str, items: list[Event], color_of) -> None:
        nonlocal y
        if not items:
            return
        row_label(title)
        for event in items:
            x = scale.x(event.ts - t0)
            rows.append(
                f'<line x1="{x:.1f}" y1="{y + 2}" x2="{x:.1f}" y2="{y + 18}" stroke="{color_of(event)}" stroke-width="2">'
                f'<title>{event.ts - t0:.3f} s {esc(event.label or event.rule)}: {esc(event.message[:200])}</title></line>'
            )
        y += row_h

    markers("启动步骤", [e for e in events if e.kind == STAGE], lambda e: "#0969da")
    markers("失败/停止", [e for e in events if e.kind in (FAILURE, SHUTDOWN, SIGNAL)], lambda e: "#cf222e" if e.kind == FAILURE else "#9a6700")
    markers("控制事件", [e for e in events if e.kind == CONTROL], lambda e: "#8250df")
    markers("控制器状态", [e for e in events if e.kind == "state"], lambda e: "#0550ae")
    markers("实验", [e for e in events if e.kind == "experiment"], lambda e: "#1a7f37")
    markers("操作台", [e for e in events if e.kind == "operator"], lambda e: "#1b7c83" if e.data.get("ok", True) else "#bf8700")
    markers("黑匣子", [e for e in events if e.rule in ("blackbox_saved", "blackbox_expected")], lambda e: "#cf222e" if e.rule == "blackbox_saved" else "#8c959f")
    markers("EtherCAT 内核", [e for e in events if e.kind == ECAT and e.source == "kernel"], lambda e: {"error": "#cf222e", "warn": "#bf8700"}.get(e.level, "#57606a"))

    # slave AL states from polling
    if ecat_rows:
        timeline: dict[int, list[tuple[float, str]]] = {}
        for record in ecat_rows:
            for item in (record.get("states") or "").split():
                pos, _, state = item.partition(":")
                timeline.setdefault(int(pos), []).append((record["t"], state))
        for pos in sorted(timeline):
            row_label(f"从站 {pos}")
            samples = [s for i, s in enumerate(timeline[pos]) if i == 0 or s[1] != timeline[pos][i - 1][1]]
            last_t = timeline[pos][-1][0]
            for index, (t, state) in enumerate(samples):
                t_next = samples[index + 1][0] if index + 1 < len(samples) else last_t + 1.0
                base = state.rstrip("E")
                x1, x2 = scale.x(t), scale.x(t_next)
                fill = STATE_COLOR.get(base, "#ffffff")
                stroke = ' stroke="#cf222e"' if state.endswith("E") else ""
                rows.append(
                    f'<rect x="{x1:.1f}" y="{y + 4}" width="{max(0.8, x2 - x1):.1f}" height="12" fill="{fill}"'
                    f'{stroke}><title>slave {pos} {esc(state)} @ {t:.2f} s</title></rect>'
                )
            y += row_h

    height = y + 24
    body = scale.axis(4, y) + "".join(rows) + "".join(labels)
    legend = "".join(f'<span><i style="background:{c}"></i>{s}</span>' for s, c in STATE_COLOR.items() if s != "?")
    return f'<svg width="{scale.width}" height="{height}" viewBox="0 0 {scale.width} {height}">{body}</svg><div class="legend">{legend}<span>竖线悬停可看详情</span></div>'


def line_chart(series: dict[str, list[tuple[float, float]]], t_max: float, unit: str, title: str, signed: bool = False) -> str:
    series = {name: [(t, v) for t, v in points if v == v] for name, points in series.items()}
    series = {name: points for name, points in series.items() if points}
    if not series:
        return '<p class="k">无采样</p>'
    scale = Scale(t_max, left=60)
    height, top = 180, 10
    values = [v for points in series.values() for _, v in points]
    if signed:
        low, high = min(values), max(values)
        pad = (high - low) * 0.05 or max(abs(high), 1e-6) * 0.05
        v_min, v_max = low - pad, high + pad
    else:
        v_min, v_max = 0.0, max(values + [1.0]) * 1.1
    span = (v_max - v_min) or 1.0

    def y_of(v: float) -> float:
        return top + height - (v - v_min) / span * height

    parts = [scale.axis(top, height)]
    for fraction in (0, 0.5, 1):
        v = v_min + span * fraction
        label = f"{v:.0f}" if not signed else f"{v:.4g}"
        parts.append(f'<line x1="60" y1="{y_of(v):.1f}" x2="{scale.width - 20}" y2="{y_of(v):.1f}" stroke="#eaeef2"/>'
                     f'<text x="54" y="{y_of(v) + 4:.1f}" text-anchor="end">{label}</text>')
    legend = []
    for index, (name, points) in enumerate(sorted(series.items(), key=lambda kv: -max(abs(v) for _, v in kv[1]))):
        color = PALETTE[index % len(PALETTE)]
        path = " ".join(f"{scale.x(t):.1f},{y_of(v):.1f}" for t, v in points)
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{path}"><title>{esc(name)}</title></polyline>')
        peak = max((v for _, v in points), key=abs)
        legend.append(f'<span><i style="background:{color}"></i>{esc(name)} 峰值 {peak:.4g}{unit}</span>' if signed else
                      f'<span><i style="background:{color}"></i>{esc(name)} 峰值 {max(v for _, v in points):.1f}{unit}</span>')
    total = height + top + 22
    return f'<div class="k">{esc(title)}</div><svg width="{scale.width}" height="{total}" viewBox="0 0 {scale.width} {total}">{"".join(parts)}</svg><div class="legend">{"".join(legend)}</div>'


def resource_series(resources: list[dict]) -> tuple[dict, dict, dict]:
    cpu: dict[str, list] = {}
    rss: dict[str, list] = {}
    threads: dict[str, list] = {}
    for sample in resources:
        for row in sample.get("procs", []):
            name = f'{row["comm"]}:{row["pid"]}'
            if row.get("cpu") is not None:
                cpu.setdefault(name, []).append((sample["t"], row["cpu"]))
            rss.setdefault(name, []).append((sample["t"], (row.get("rss_kib") or 0) / 1024.0))
        for tid, name, _cpu_index, value in sample.get("threads", []):
            if value is not None:
                threads.setdefault(f"{name}:{tid}", []).append((sample["t"], value))
    top_threads = dict(sorted(threads.items(), key=lambda kv: -max(v for _, v in kv[1]))[:8])
    return cpu, rss, top_threads


def counters_table(ecat_rows: list[dict]) -> str:
    rows = [r for r in ecat_rows if r.get("counters")]
    if not rows:
        return '<p class="k">本次运行未轮询主站（仿真硬件，或轮询频率为 0）。</p>'
    first, last = rows[0]["counters"], rows[-1]["counters"]
    errors = [r for r in ecat_rows if r.get("error")]
    cells = "".join(
        f'<tr><td>{esc(key)}</td><td class="num">{first.get(key, "-")}</td><td class="num">{last.get(key, "-")}</td>'
        f'<td class="num">{(last.get(key, 0) - first.get(key, 0)) if key in first else "-"}</td></tr>'
        for key in ("tx_frames", "rx_frames", "lost_frames", "tx_errors")
    )
    note = f'<p class="k">轮询失败 {len(errors)} 次（例如主站释放后）。</p>' if errors else ""
    return f'<table><tr><th>计数</th><th>首次采样</th><th>末次采样</th><th>本次运行增量</th></tr>{cells}</table>{note}<p class="k">采样 {len(rows)} 次，{rows[0]["t"]:.1f}–{rows[-1]["t"]:.1f} s。</p>'


def send_trace_section(run: dict) -> str:
    trace = run.get("send_trace")
    if not trace:
        return ""
    rows = "".join(
        f'<tr class="{"hit" if p.get("gaps_below_burst") else ""}"><td>{esc(name)}</td><td class="num">{p.get("samples", 0)}</td>'
        f'<td class="num">{p.get("min_gap_ns", 0) / 1e3:.1f}</td><td class="num">{p.get("max_gap_ns", 0) / 1e3:.1f}</td>'
        f'<td class="num">{p.get("gaps_below_burst", 0)}</td><td class="num">{p.get("gaps_above_1_5x", 0)}</td><td class="num">{p.get("wkc_incomplete", 0)}</td></tr>'
        for name, p in (trace.get("phases") or {}).items()
    )
    window = trace.get("handoff_window") or []
    cells = " ".join(f'{w["phase"][0]}{w["t_us"]:+.0f}' for w in window)
    return (
        '<h2>EtherCAT 发送轨迹（交接取证）</h2><table><tr><th>阶段</th><th>发送次数</th><th>最小间隔 µs</th><th>最大间隔 µs</th>'
        f'<th>&lt;500 µs（连发）</th><th>&gt;1.5 ms</th><th>WKC 不完整</th></tr>{rows}</table>'
        f'<p class="k mono">交接前后发送时刻（s=启动 m=交接 c=控制，相对第一帧控制 µs）：{esc(cells)}</p>'
    )


def post_stop_section(run: dict) -> str:
    result = run.get("post_stop")
    if not result:
        return '<p class="k">未读取（仿真硬件、记录中断或未启用）。</p>'
    if result.get("skipped"):
        return f'<p class="k">未读取：{esc(result["skipped"])}</p>'
    rows = "".join(
        f'<tr class="{"hit" if d.get("fault") else ("warn" if d.get("error_code") else "")}"><td class="num">{d["position"]}</td><td>{esc(d["name"])}</td><td>{esc(d.get("al_state"))}</td>'
        f'<td class="mono">{esc(d.get("error_code_hex") or d.get("error_code_error", "-"))}</td><td>{esc(d.get("error_text", ""))}</td>'
        f'<td class="mono">{esc(d.get("status_word_hex") or d.get("status_word_error", "-"))}</td><td>{"是" if d.get("fault") else ""}</td></tr>'
        for d in result.get("drives", [])
    )
    return (f'<p class="k">读取时主站 {esc(result.get("master_phase"))}，{fmt_time(result.get("time"))}。</p>'
            f'<table><tr><th>位置</th><th>从站</th><th>AL</th><th>0x603F</th><th>含义</th><th>0x6041</th><th>Fault</th></tr>{rows}</table>')


def blackbox_section(run: dict, events: list[Event], t0: float) -> str:
    boxes = run.get("blackbox") or []
    if not boxes:
        return '<p class="k">本次运行没有 1 kHz 记录控制器（未加载 runtime_log_recorder）。</p>'
    rows = []
    for box in boxes:
        stats = box.get("cycle_stats") or {}
        nominal = stats.get("nominal_period_ns")
        rows.append(
            f'<tr><td class="mono">{esc(box.get("segment"))}</td><td class="num">{box.get("fields")}</td><td class="num">{stats.get("cycles", 0)}</td>'
            f'<td class="num">{(nominal or 0) / 1e3:.1f}</td><td class="num">{(stats.get("mean_period_ns") or 0) / 1e3:.3f}</td>'
            f'<td class="num">{(stats.get("min_period_ns") or 0) / 1e3:.1f} / {(stats.get("max_period_ns") or 0) / 1e3:.1f}</td>'
            f'<td class="num">{stats.get("overruns", 0)}</td><td class="num">{box.get("gaps", 0)}</td>'
            f'<td class="num">{len(box.get("saved") or [])}</td><td class="num">{len(box.get("expected_transitions") or [])}</td></tr>'
        )
    states = {}
    for box in boxes:
        states.update(box.get("drive_states") or {})
    state_rows = "".join(
        f'<tr class="{"hit" if s["state"] in ("fault", "fault_reaction_active", "quick_stop_active") else ""}"><td>{esc(joint)}</td><td>{esc(s["label"])}</td>'
        f'<td class="mono">{esc(s["word"])}</td><td>{"有" if s["voltage_enabled"] else "无"}</td><td>{"是" if s["warning"] else ""}</td></tr>'
        for joint, s in sorted(states.items())
    )
    saved = [e for e in events if e.rule == "blackbox_saved"]
    saved_rows = "".join(
        f'<tr class="hit"><td class="num">{e.ts - t0:.3f}</td><td>{esc(", ".join(e.data.get("reasons", [])))}</td><td class="mono">{esc(e.message[:200])}</td>'
        f'<td><a href="blackbox/{esc(e.data.get("dir"))}/#waveforms">查看波形</a> · <a href="blackbox/{esc(e.data.get("dir"))}/window.mcap">window.mcap</a> · <a href="blackbox/{esc(e.data.get("dir"))}/window.csv">CSV</a> · <a href="blackbox/{esc(e.data.get("dir"))}/trigger.json">trigger.json</a>'
        f' · <a href="blackbox/{esc(e.data.get("dir"))}/kernel.log">kernel.log</a> · <a href="blackbox/{esc(e.data.get("dir"))}/timeline.json">timeline.json</a></td></tr>'
        for e in saved
    )
    return (
        '<table><tr><th>共享内存段</th><th>字段</th><th>周期数</th><th>名义周期 µs</th><th>平均 µs</th><th>最小 / 最大 µs</th>'
        '<th>超时（&gt;1.5 倍）</th><th>读者缺口</th><th>已存窗口</th><th>预期内切换</th></tr>' + "".join(rows) + "</table>"
        + (f'<br><table style="max-width:640px"><tr><th>关节</th><th>驱动状态（最后读到）</th><th>状态字</th><th>动力电</th><th>警告</th></tr>{state_rows}</table>' if state_rows else "")
        + (f'<br><table><tr><th>t (s)</th><th>触发原因</th><th>详情</th><th>文件（前 10 s、后 2 s）</th></tr>{saved_rows}</table>' if saved_rows else '<p class="k">没有触发。</p>')
    )


def phase_counters(summary: dict) -> str:
    by_phase = summary.get("ecat_counters_by_phase") or {}
    if not by_phase:
        return ""
    names = {"startup": "启动", "running": "运行", "stopping": "停止"}
    rows = "".join(f'<tr class="{"hit" if v["lost_frames"] or v["tx_errors"] else ""}"><td>{names.get(k, k)}</td><td class="num">{v["lost_frames"]}</td>'
                   f'<td class="num">{v["tx_errors"]}</td></tr>' for k, v in by_phase.items())
    return f'<br><table style="max-width:420px"><tr><th>阶段</th><th>丢帧增量</th><th>发送错误增量</th></tr>{rows}</table>'


def render(run: dict, events: list[Event], run_dir: Optional[Path] = None) -> str:
    from .experiment_report import experiments_section  # local: experiment_report imports this module

    summary = run.get("summary") or {}
    t0 = summary.get("t0") or run.get("start") or 0.0
    t_max = max(summary.get("duration_s") or 0.0, 1.0)
    resources = read_jsonl(run_dir / "resources.jsonl") if run_dir else []
    ecat_rows = read_jsonl(run_dir / "ethercat.jsonl") if run_dir else []
    if resources:
        t_max = max(t_max, resources[-1]["t"])
    result = run.get("result", "?")

    failure = summary.get("failure")
    last = summary.get("last_stage") or {}
    failure_html = ""
    if failure:
        failure_html = (
            f'<div class="card fail"><b>失败步骤：{esc(summary.get("failed_stage"))}</b> — {esc(failure.get("label"))}'
            f'<br><span class="k">{fmt_s(failure.get("t_rel_s"))} · {esc(failure.get("subject"))}</span>'
            f'<div class="mono">{esc(failure.get("message"))}</div>'
            f'<div class="k">最后成功的启动步骤：{esc(last.get("label", "无"))}（{fmt_s(last.get("t_rel_s"))}）</div></div>'
        )

    artifact = run.get("artifact") or {}
    selection = artifact.get("selection") or {}
    facts = [
        ("开始", fmt_time(run.get("start"))),
        ("时长", fmt_s(summary.get("duration_s"))),
        ("就绪耗时", fmt_s(summary.get("ready_s"))),
        ("ROS_DOMAIN_ID", f'{run.get("domain")}（来自主机配置）' if run.get("domain_source") == "host.yaml"
         else run.get("domain")),
        ("scope", run.get("scope") or selection.get("control_scope")),
        ("硬件", run.get("hardware")),
        ("机器单元", selection.get("machine_unit")),
        ("控制周期", f'{artifact["update_rate"]} Hz' if artifact.get("update_rate") else "-"),
        ("launch pid", run.get("launch_pid")),
        ("来源", run.get("source")),
        ("峰值 CPU", f'{(run.get("resources") or {}).get("peak_cpu_percent", "-")} %'),
        ("峰值 RSS", f'{(run.get("resources") or {})["peak_rss_kib"] / 1024:.0f} MiB' if (run.get("resources") or {}).get("peak_rss_kib") else "-"),
    ]
    facts_html = "".join(f'<div><div class="k">{esc(k)}</div><div class="v">{esc(v)}</div></div>' for k, v in facts)

    anomalies = summary.get("anomalies") or {}
    anomaly_rows = "".join(
        f'<tr class="{"" if is_anomaly_key_informational(k) else "hit"}"><td>{esc(k)}</td><td class="num">{v}</td></tr>' for k, v in anomalies.items()
    ) or '<tr><td colspan="2">无</td></tr>'

    stage_rows = "".join(
        f'<tr><td class="num">{e.ts - t0:.3f}</td><td>{esc(e.label)}</td><td class="mono">{esc(e.message[:160])}</td></tr>'
        for e in events
        if e.kind == STAGE
    )

    cpu, rss, threads = resource_series(resources)
    thread_info = (run.get("resources") or {}).get("threads") or []
    thread_rows = "".join(
        f'<tr><td class="num">{t["tid"]}</td><td>{esc(t["name"])}</td><td>{esc(t["policy"])}</td><td class="num">{t["rt_priority"]}</td>'
        f'<td>{esc(t.get("affinity") or "-")}</td><td>{esc(",".join(map(str, sorted(t["cpus_seen"]))))}</td><td class="num">{t.get("peak_cpu", 0):.1f}</td><td class="num">{t.get("first_seen_s", 0):.1f}</td></tr>'
        for t in thread_info
    )

    event_rows = []
    for event in events:
        css = {"error": "error", "warn": "warn"}.get(event.level, "")
        if event.kind == FAILURE:
            css = "hit"
        event_rows.append(
            f'<tr class="{css}" data-level="{event.level}" data-kind="{event.kind}"><td class="num">{event.ts - t0:.3f}</td><td>{esc(event.kind)}</td>'
            f'<td>{esc(event.source)}</td><td>{esc(event.subject)}</td><td>{esc(event.label)}</td><td class="mono">{esc(event.message[:300])}</td></tr>'
        )

    controllers = artifact.get("controllers") or {}
    controller_rows = "".join(f'<tr><td>{esc(name)}</td><td class="mono">{esc(kind)}</td></tr>' for name, kind in controllers.items())
    git_rows = "".join(
        f'<tr><td class="mono">{esc(g.get("path"))}</td><td>{esc(g.get("branch"))}</td><td class="mono">{esc((g.get("head") or "")[:12])}</td>'
        f'<td class="num">{esc(g.get("dirty_files"))}</td><td class="mono">{esc((g.get("diff_sha256") or "")[:12])}</td></tr>'
        for g in run.get("git") or []
    )
    hashes = "".join(f'<tr><td>{esc(k)}</td><td class="mono">{esc(v)}</td></tr>' for k, v in (artifact.get("sha256") or {}).items())
    meta_json = esc(json.dumps({k: run.get(k) for k in ("launch_args", "launch_cmdline", "parameter_overrides", "host", "polling", "rmw", "ros_log_dir")}, ensure_ascii=False, indent=1))

    window = startup_window(summary)
    zoom = f"<h2>时间线（启动放大，0–{window:.1f} s）</h2>{gantt(summary, events, ecat_rows, t0, window)}" if window else ""
    color = RESULT_COLOR.get(result, "#57606a")
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(run.get("id"))} · Runtime-LOG</title><style>{CSS}</style></head><body><main>
<p><a href="../">← 全部运行</a></p>
<h1>{esc(run.get("id"))} <span class="badge" style="background:{color}">{esc(RESULT_LABEL.get(result, result))}</span></h1>
{failure_html}
<div class="card grid">{facts_html}</div>
<h2>异常计数</h2><table style="max-width:520px"><tr><th>项目</th><th>次数</th></tr>{anomaly_rows}</table>
<h2>1 kHz 黑匣子</h2>{blackbox_section(run, events, t0)}
<h2>1 kHz 性能实验</h2>{experiments_section(run, run_dir)}
<h2>停后驱动错误码</h2>{post_stop_section(run)}
{send_trace_section(run)}
{zoom}<h2>时间线（全程）</h2>{gantt(summary, events, ecat_rows, t0, t_max)}
<h2>启动步骤</h2><table><tr><th>t (s)</th><th>步骤</th><th>原文</th></tr>{stage_rows or '<tr><td colspan="3">无</td></tr>'}</table>
<h2>资源</h2>{line_chart(cpu, t_max, "%", "进程 CPU（%，单核 = 100）")}{line_chart(rss, t_max, " MiB", "进程常驻内存（MiB）")}{line_chart(threads, t_max, "%", "ros2_control_node 线程 CPU（前 8）")}
<h2>ros2_control_node 线程</h2><table><tr><th>tid</th><th>名称</th><th>策略</th><th>RT 优先级</th><th>允许 CPU</th><th>实际运行 CPU</th><th>峰值 CPU %</th><th>首次出现 (s)</th></tr>{thread_rows or '<tr><td colspan="8">无采样</td></tr>'}</table>
<h2>EtherCAT 主站计数</h2>{counters_table(ecat_rows)}{phase_counters(summary)}
<h2>事件</h2><div class="filters"><label><input type="checkbox" id="onlyIssues"> 只看 WARN/ERROR</label><label><input type="checkbox" id="hideControl"> 隐藏控制事件</label></div>
<table id="events"><tr><th>t (s)</th><th>类型</th><th>来源</th><th>对象</th><th>规则</th><th>内容</th></tr>{"".join(event_rows)}</table>
<h2>部署与版本</h2>
<table><tr><th>控制器</th><th>类型</th></tr>{controller_rows or '<tr><td colspan="2">-</td></tr>'}</table><br>
<table><tr><th>仓库</th><th>分支</th><th>HEAD</th><th>未提交文件</th><th>diff sha256</th></tr>{git_rows or '<tr><td colspan="5">-</td></tr>'}</table><br>
<table><tr><th>部署文件</th><th>sha256</th></tr>{hashes or '<tr><td colspan="2">-</td></tr>'}</table>
<details><summary>启动参数与主机</summary><pre class="mono">{meta_json}</pre></details>
<p class="k">生成于 {fmt_time(time.time())} · Runtime-LOG</p>
</main><script>
const rows=[...document.querySelectorAll('#events tr[data-level]')];
function apply(){{const issues=document.getElementById('onlyIssues').checked,hide=document.getElementById('hideControl').checked;
rows.forEach(r=>{{const lv=r.dataset.level,k=r.dataset.kind;r.style.display=((issues&&lv==='info'&&k!=='failure')||(hide&&k==='control'))?'none':''}})}}
document.getElementById('onlyIssues').onchange=apply;document.getElementById('hideControl').onchange=apply;
</script></body></html>"""


def write_report(run_dir: Path, run: dict, events: list[Event]) -> Path:
    target = run_dir / "report.html"
    temporary = run_dir / ".report.html.tmp"
    temporary.write_text(render(run, events, run_dir), encoding="utf-8")
    temporary.replace(target)
    return target


__all__ = ["render", "write_report", "ERROR", "WARNING", "PROCESS_EXITED"]
