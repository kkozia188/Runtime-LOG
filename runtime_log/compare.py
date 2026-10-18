"""Side-by-side comparison of two archived runs (read-only page)."""

from __future__ import annotations

import html
import json
from typing import Any

from .model import Event
from .report import CSS, RESULT_COLOR, RESULT_LABEL, fmt_s, fmt_time


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def flatten(value: Any, prefix: str = "") -> dict:
    """Nested dict/list -> {"a.b[0]": leaf}."""
    out: dict = {}
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list) and value and all(isinstance(item, (dict, list)) for item in value):
        for index, item in enumerate(value):
            out.update(flatten(item, f"{prefix}[{index}]"))
    else:
        out[prefix] = value
    return out


def stage_times(run: dict, events: list[Event]) -> dict:
    t0 = (run.get("summary") or {}).get("t0") or run.get("start") or 0
    times: dict = {}
    for event in events:
        if event.kind == "stage" and event.rule not in times:
            times[event.rule] = (event.label or event.rule, round(event.ts - t0, 3))
    return times


def _cell(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, dict)):
        return esc(json.dumps(value, ensure_ascii=False)[:160])
    return esc(value)


def table(title: str, rows: list[tuple[str, Any, Any]], only_differences: bool = False, note: str = "") -> str:
    body = []
    differing = 0
    for key, a, b in rows:
        same = a == b
        differing += not same
        if only_differences and same:
            continue
        body.append(f'<tr class="{"" if same else "warn"}"><td class="mono">{esc(key)}</td><td>{_cell(a)}</td><td>{_cell(b)}</td></tr>')
    summary = f'<p class="k">{len(rows)} 项，{differing} 项不同。{esc(note)}</p>'
    if not body:
        return f"<h2>{esc(title)}</h2>{summary}"
    return f'<h2>{esc(title)}</h2>{summary}<table><tr><th>项目</th><th>A</th><th>B</th></tr>{"".join(body)}</table>'


def keyed(a: dict, b: dict) -> list[tuple[str, Any, Any]]:
    return [(key, a.get(key), b.get(key)) for key in sorted(set(a) | set(b))]


def render_compare(run_a: dict, events_a: list[Event], run_b: dict, events_b: list[Event]) -> str:
    def head(run: dict, tag: str) -> str:
        result = run.get("result", "?")
        color = RESULT_COLOR.get(result, "#57606a")
        summary = run.get("summary") or {}
        return (
            f'<div class="card"><b>{tag}</b> <a href="/{esc(run.get("id"))}/">{esc(run.get("id"))}</a> '
            f'<span class="badge" style="background:{color}">{esc(RESULT_LABEL.get(result, result))}</span>'
            f'<div class="k">{fmt_time(run.get("start"))} · {esc(run.get("scope"))} · 域 {esc(run.get("domain"))} · {esc(run.get("hardware"))}'
            f' · 时长 {fmt_s(summary.get("duration_s"))} · 就绪 {fmt_s(summary.get("ready_s"))}</div></div>'
        )

    sections = [head(run_a, "A"), head(run_b, "B")]

    def git_map(run: dict) -> dict:
        out = {}
        for repo in run.get("git") or []:
            name = (repo.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
            for key in ("branch", "head", "dirty_files", "diff_sha256"):
                out[f"{name}.{key}"] = repo.get(key)
        return out

    art_a, art_b = run_a.get("artifact") or {}, run_b.get("artifact") or {}
    sections.append(table("代码版本", keyed(git_map(run_a), git_map(run_b))))
    sections.append(table("部署产物（文件哈希与选择）", keyed(
        {**{f"sha256.{k}": v for k, v in (art_a.get("sha256") or {}).items()}, **{f"selection.{k}": v for k, v in (art_a.get("selection") or {}).items()},
         "update_rate": art_a.get("update_rate"), "controllers": art_a.get("controllers")},
        {**{f"sha256.{k}": v for k, v in (art_b.get("sha256") or {}).items()}, **{f"selection.{k}": v for k, v in (art_b.get("selection") or {}).items()},
         "update_rate": art_b.get("update_rate"), "controllers": art_b.get("controllers")},
    )))
    sections.append(table("控制器参数（只列不同）", keyed(flatten(art_a.get("controller_parameters") or {}), flatten(art_b.get("controller_parameters") or {})),
                          only_differences=True))
    sections.append(table("启动参数与覆盖", keyed(
        {**{f"launch.{k}": v for k, v in (run_a.get("launch_args") or {}).items()}, **{f"-p {k}": v for k, v in (run_a.get("parameter_overrides") or {}).items()},
         **{f"host.{k}": v for k, v in (run_a.get("host") or {}).items() if k != "runtime_log_version"}, **{f"polling.{k}": v for k, v in (run_a.get("polling") or {}).items()}},
        {**{f"launch.{k}": v for k, v in (run_b.get("launch_args") or {}).items()}, **{f"-p {k}": v for k, v in (run_b.get("parameter_overrides") or {}).items()},
         **{f"host.{k}": v for k, v in (run_b.get("host") or {}).items() if k != "runtime_log_version"}, **{f"polling.{k}": v for k, v in (run_b.get("polling") or {}).items()}},
    )))

    times_a, times_b = stage_times(run_a, events_a), stage_times(run_b, events_b)
    stage_rows = []
    for rule in sorted(set(times_a) | set(times_b), key=lambda r: (times_a.get(r) or times_b.get(r))[1]):
        label = (times_a.get(rule) or times_b.get(rule))[0]
        a, b = (times_a.get(rule) or (None, None))[1], (times_b.get(rule) or (None, None))[1]
        delta = f"{b - a:+.3f}" if a is not None and b is not None else "-"
        big = a is not None and b is not None and abs(b - a) > 0.5
        stage_rows.append(f'<tr class="{"warn" if big or a is None or b is None else ""}"><td>{esc(label)}</td><td class="num">{fmt_s(a)}</td>'
                          f'<td class="num">{fmt_s(b)}</td><td class="num">{delta}</td></tr>')
    sections.append('<h2>启动步骤（相对 launch 开始）</h2><table><tr><th>步骤</th><th>A</th><th>B</th><th>B − A</th></tr>'
                    + "".join(stage_rows) + "</table>")

    anomalies_a = (run_a.get("summary") or {}).get("anomalies") or {}
    anomalies_b = (run_b.get("summary") or {}).get("anomalies") or {}
    sections.append(table("异常计数", keyed(anomalies_a, anomalies_b)))

    def cycles(run: dict) -> dict:
        out = {}
        for box in run.get("blackbox") or []:
            for key, value in (box.get("cycle_stats") or {}).items():
                out[f"{box.get('writer')}.{key}"] = value
            out[f"{box.get('writer')}.gaps"] = box.get("gaps")
            out[f"{box.get('writer')}.saved_windows"] = len(box.get("saved") or [])
        trace = run.get("send_trace") or {}
        for phase, values in (trace.get("phases") or {}).items():
            for key in ("samples", "min_gap_ns", "max_gap_ns", "gaps_below_burst"):
                out[f"send.{phase}.{key}"] = values.get(key)
        return out

    sections.append(table("1 kHz 周期与发送轨迹", keyed(cycles(run_a), cycles(run_b))))

    def threads(run: dict) -> dict:
        out = {}
        for thread in (run.get("resources") or {}).get("threads") or []:
            key = f'{thread["name"]} {thread["policy"]}/{thread["rt_priority"]}'
            out.setdefault(key, set()).add(thread.get("affinity") or "-")
        out = {key: ",".join(sorted(values)) for key, values in out.items()}
        out["peak_cpu_percent"] = (run.get("resources") or {}).get("peak_cpu_percent")
        out["peak_rss_mib"] = round(((run.get("resources") or {}).get("peak_rss_kib") or 0) / 1024)
        return out

    sections.append(table("线程布局与资源（名称 策略/优先级 → 允许 CPU）", keyed(threads(run_a), threads(run_b))))

    def post_stop(run: dict) -> dict:
        result = run.get("post_stop") or {}
        out = {f'slave {d["position"]}': f'{d.get("error_code_hex", "-")} / {d.get("status_word_hex", "-")}' for d in result.get("drives", [])}
        if result.get("skipped"):
            out["skipped"] = result["skipped"]
        return out

    sections.append(table("停后错误码（0x603F / 0x6041）", keyed(post_stop(run_a), post_stop(run_b))))
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>对比 · Runtime-LOG</title><style>{CSS}</style></head>'
            f'<body><main><p><a href="/">← 全部运行</a></p><h1>两次运行对比</h1>{"".join(sections)}</main></body></html>')
