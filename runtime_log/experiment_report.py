"""Pages for M2 experiments: list in the run report, detail page, two-experiment comparison."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Optional

from .report import CSS, fmt_s, fmt_time, line_chart
from .waveform_view import render_waveform

METRICS = [
    ("travel_deg", "行程 °"),
    ("motion_time_s", "运动时长 s"),
    ("max_error_deg", "最大跟随误差 °"),
    ("rms_error_moving_deg", "运动中 RMS 误差 °"),
    ("overshoot_deg", "超调 °"),
    ("settle_time_s", "到位时间 s"),
    ("final_error_deg", "终点误差 °"),
    ("static_jitter_deg", "静止抖动 °"),
    ("peak_velocity_deg_s", "峰值速度 °/s"),
    ("peak_torque_permille", "峰值力矩 ‰"),
]


VELOCITY_METRICS = [
    ('max_error_rad_s', '最大速度误差 rad/s'), ('rms_error_rad_s', 'RMS 速度误差 rad/s'),
    ('rms_error_active_rad_s', '运动中 RMS 速度误差 rad/s'), ('mean_error_rad_s', '平均速度误差 rad/s'),
    ('peak_target_rad_s', '峰值已发速度 rad/s'), ('peak_actual_rad_s', '峰值实际速度 rad/s'),
    ('max_feedback_age_ms', '最大反馈年龄 ms'),
]


def velocity_section(meta: dict) -> str:
    velocity = meta.get('velocity_joints') or {}
    if not velocity:
        return ''
    rows = []
    for name, m in sorted(velocity.items()):
        if not m.get('csv_samples'):
            continue
        cells = ''.join(f'<td class="num">{_value(m[k]) if m.get(k) is not None else "—"}</td>' for k, _ in VELOCITY_METRICS)
        rows.append(f'<tr><td>{esc(name)}</td><td>{m["samples"]} / {m["total_samples"]}</td>{cells}'
                    f'<td>{esc(json.dumps(m["excluded"], ensure_ascii=False))}</td></tr>')
    columns = ''.join(f'<th>{esc(label)}</th>' for _, label in VELOCITY_METRICS)
    settings = meta.get('velocity_settings') or {}
    return ('<h2>行走轮速度性能</h2><p class="k">误差 = 已发速度 − 实际速度，单位 rad/s。'
            f'仅统计模式9（CSV）、运行使能、反馈年龄 ≤ {settings.get("feedback_limit_ms", 20)} ms 且适配器有效的采样。'
            f'任一目标或实际速度绝对值 > {settings.get("active_speed_rad_s", 0.01)} rad/s 视为运动。'
            '未使能时仍记录原始波形，但不报告跟随性能；没有有效采样显示“—”。'
            '不使用轮速里程计冒充真实地面速度，不推算转向误差、响应延迟或超调。</p>'
            '<table><tr><th>轴</th><th>有效 / 总采样</th>' + columns + '<th>排除原因计数</th></tr>'
            + ''.join(rows) + '</table>')


def summary_error(summary: dict) -> str:
    if summary.get('tracking_kind') == 'velocity':
        value = summary.get('max_error_rad_s')
        return '无有效 CSV 样本' if value is None else f'{value:.5g} rad/s'
    value = summary.get('max_error_deg')
    return '—' if value is None else f'{value:.5g} °'


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _value(value) -> str:
    if value is None:
        return "未到位"
    if isinstance(value, float):
        return f"{value:g}"
    return esc(value)


# Picks are remembered across pages (localStorage) so experiments of different runs can be compared.
EXPERIMENT_PICKER = """<p><a id="expCompare" href="#" style="display:none">对比所选两次实验</a> <span class="k" id="expPicked"></span></p>
<script>
(function(){const key='rl_exp_picks';const load=()=>JSON.parse(localStorage.getItem(key)||'[]');
function show(){const p=load();const a=document.getElementById('expCompare');document.getElementById('expPicked').textContent=p.length?'已选：'+p.join('，'):'勾选两次实验后对比（可跨运行）';
a.style.display=p.length===2?'':'none';if(p.length===2)a.href='/compare_experiments?a='+encodeURIComponent(p[0])+'&b='+encodeURIComponent(p[1]);
document.querySelectorAll('.exp-pick').forEach(c=>c.checked=p.includes(c.value));}
document.addEventListener('change',e=>{if(!e.target.classList.contains('exp-pick'))return;let p=load().filter(x=>x!==e.target.value);
if(e.target.checked){p.push(e.target.value);while(p.length>2)p.shift();}localStorage.setItem(key,JSON.stringify(p));show();});
document.addEventListener('DOMContentLoaded',show);show();})();
</script>"""


def experiments_of(run: dict) -> list[dict]:
    rows = []
    for box in run.get("blackbox") or []:
        rows.extend(box.get("experiments") or [])
    return sorted(rows, key=lambda r: r.get("start") or 0)


def experiments_on_disk(run_dir: Optional[Path]) -> list[dict]:
    rows = []
    if run_dir is None:
        return rows
    for meta_path in sorted((run_dir / "experiments").glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({"dir": meta_path.parent.name, "name": meta.get("name"), "source": meta.get("source"),
                     "start": meta.get("start"), "duration_s": meta.get("duration_s", 0), "summary": meta.get("summary") or {}})
    return rows


def experiments_section(run: dict, run_dir: Optional[Path]) -> str:
    rows = experiments_on_disk(run_dir) or experiments_of(run)
    active = [box.get('active_experiment') for box in run.get('blackbox') or [] if box.get('active_experiment')]
    active_note = ''.join(f'<p class="k">正在记录：{esc(e["name"])} · {e["cycles"]} 个采样；'
                          '分段完成或停栈收尾后显示下载和波形入口。</p>' for e in active)
    for box in run.get('blackbox') or []:
        c = box.get('continuous') or {}
        if c.get('configured') and (c.get('paused') or c.get('disk_ok') is False):
            active_note += '<p class="warn">持续记录已暂停：手动停止或磁盘余量不足。</p>'
    if not rows:
        return active_note + '<p class="k">没有已完成的实验。机械臂 JTC 可自动记录，底盘可启用持续分段记录；也可用 <code>runtime-log experiment start/stop</code> 手动记录。</p>'
    t0 = (run.get("summary") or {}).get("t0") or run.get("start") or 0
    body = []
    for row in rows:
        summary = row.get("summary") or {}
        meta = {}
        if run_dir is not None:
            try:
                meta = json.loads((run_dir / "experiments" / row["dir"] / "meta.json").read_text()).get("meta", {})
            except (OSError, ValueError):
                meta = {}
        unsettled = summary.get("unsettled") or []
        body.append(
            f'<tr class="{"warn" if unsettled else ""}"><td><input type="checkbox" class="exp-pick" value="{esc(run.get("id"))}/{esc(row["dir"])}"></td>'
            f'<td class="num">{(row.get("start") or t0) - t0:.3f}</td>'
            f'<td><a href="experiments/{esc(row["dir"])}/">{esc(row["name"])}</a></td><td>{esc(row.get("source"))}</td>'
            f'<td>{esc(meta.get("intent", ""))}</td><td class="num">{row.get("duration_s", 0):.2f}</td>'
            f'<td class="num">{summary.get("moving_joints", 0)}</td>'
            f'<td class="num">{summary_error(summary)} {esc(summary.get("max_error_joint") or "")}</td>'
            f'<td class="num">{_value(summary.get("worst_settle_s")) if summary.get("moving_joints") and summary.get("tracking_kind") != "velocity" else "—"}</td>'
            f'<td class="num">{_value(summary.get("max_overshoot_deg")) if summary.get("tracking_kind") != "velocity" else "—"}</td><td>{esc(", ".join(unsettled))}</td>'
            f'<td><a href="experiments/{esc(row["dir"])}/#waveforms">查看波形</a> · <a href="experiments/{esc(row["dir"])}/data.mcap">data.mcap</a> · <a href="experiments/{esc(row["dir"])}/data.csv">CSV</a></td></tr>'
        )
    return (active_note + EXPERIMENT_PICKER + '<table><tr><th></th><th>t (s)</th><th>实验</th><th>来源</th><th>操作</th><th>时长 s</th><th>运动关节</th><th>最大跟随误差（按类型）</th>'
            '<th>最慢到位 s</th><th>最大超调 °</th><th>未到位</th><th>数据</th></tr>' + "".join(body) + "</table>")


def render_experiment(run_id: str, meta: dict, plot: dict) -> str:
    joints = meta.get("joints") or {}
    rows = "".join(
        f'<tr class="{"" if m.get("moved") else "k"}"><td>{esc(joint)}</td>' + "".join(f'<td class="num">{_value(m.get(key)) if key in m else "-"}</td>' for key, _ in METRICS) + "</tr>"
        for joint, m in sorted(joints.items())
    )
    header = "".join(f"<th>{esc(label)}</th>" for _, label in METRICS)
    charts = []
    shown = [joint for joint, m in sorted(joints.items()) if m.get("moved")] or sorted(plot)[:2]
    for joint in shown:
        data = plot.get(joint)
        if not data:
            continue
        t_max = data["t"][-1] if data["t"] else 1.0
        charts.append(f"<h3>{esc(joint)}</h3>")
        charts.append(line_chart({"目标（实际发出）": list(zip(data["t"], data["target_deg"])), "实际位置": list(zip(data["t"], data["position_deg"]))},
                                 t_max, "°", "位置（°）", signed=True))
        charts.append(line_chart({"跟随误差": list(zip(data["t"], data["error_deg"]))}, t_max, "°", "跟随误差（目标 − 实际，°）", signed=True))
    settings = meta.get("settings") or {}
    info = meta.get("meta") or {}
    channels = meta.get('waveform_channels') or sorted(joints)
    preferred = shown or [j for j, m in sorted((meta.get('velocity_joints') or {}).items()) if m.get('csv_samples')]
    channels = preferred + [j for j in channels if j not in preferred]
    metric_section = f'<h2>逐关节指标</h2><table><tr><th>关节</th>{header}</tr>{rows}</table>' if joints else ''
    metric_section += velocity_section(meta)
    explanation = (f'到位：终点误差小于 {settings.get("settle_tolerance_deg")}° 并保持 {settings.get("settle_hold_s")} s；'
                   '位置跟随误差 = 实际发给驱动的目标位置 − 实际位置。') if joints else '底盘实验：行走轮速度、转向位置、外置编码器与总线状态。'
    return (
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(meta.get("name"))} · 实验</title><style>{CSS}</style></head><body><main>'
        f'<p><a href="../../">← 运行 {esc(run_id)}</a></p><h1>实验 {esc(meta.get("name"))}</h1>'
        f'<div class="card grid"><div><div class="k">开始</div><div class="v">{fmt_time(meta.get("start"))}</div></div>'
        f'<div><div class="k">时长</div><div class="v">{fmt_s(meta.get("duration_s"))}</div></div>'
        f'<div><div class="k">来源</div><div class="v">{esc(meta.get("source"))}</div></div>'
        f'<div><div class="k">操作</div><div class="v">{esc(info.get("intent", ""))}</div></div>'
        f'<div><div class="k">周期数 / 缺失</div><div class="v">{meta.get("cycles")} / {meta.get("missing_cycles")}</div></div>'
        f'<div><div class="k">周期 p99 / 最大 µs</div><div class="v">{(meta.get("period_us") or {}).get("p99")} / {(meta.get("period_us") or {}).get("max")}</div></div></div>'
        f'<p class="k">{explanation}'
        f' 数据：<a href="#waveforms">查看波形</a> · <a href="data.csv">CSV（PlotJuggler / Excel）</a> · <a href="data.mcap">MCAP 归档</a> · <a href="meta.json">meta.json</a></p>'
        f'{render_waveform(channels)}'
        f'{metric_section}'
        f'{"<h2>位置概览</h2>" + "".join(charts) if charts else ""}'
        f'</main></body></html>'
    )


def render_experiment_compare(a: tuple[str, dict], b: tuple[str, dict]) -> str:
    (name_a, meta_a), (name_b, meta_b) = a, b
    joints = sorted(set(meta_a.get("joints", {})) | set(meta_b.get("joints", {})))
    rows = []
    for joint in joints:
        ma, mb = meta_a.get("joints", {}).get(joint, {}), meta_b.get("joints", {}).get(joint, {})
        if not (ma.get("moved") or mb.get("moved")):
            continue
        for key, label in METRICS:
            va, vb = ma.get(key), mb.get(key)
            if key not in ma and key not in mb:
                continue
            delta = f"{vb - va:+.4g}" if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else "-"
            rows.append(f'<tr><td>{esc(joint)}</td><td>{esc(label)}</td><td class="num">{_value(va)}</td><td class="num">{_value(vb)}</td><td class="num">{delta}</td></tr>')
    va, vb = meta_a.get('velocity_joints') or {}, meta_b.get('velocity_joints') or {}
    for joint in sorted(set(va) | set(vb)):
        ma, mb = va.get(joint, {}), vb.get(joint, {})
        if not (ma.get('csv_samples') or mb.get('csv_samples')):
            continue
        for key, label in VELOCITY_METRICS:
            a_value, b_value = ma.get(key), mb.get(key)
            delta = f'{b_value - a_value:+.4g}' if a_value is not None and b_value is not None else '—'
            rows.append(f'<tr><td>{esc(joint)}</td><td>{esc(label)}</td>'
                        f'<td class="num">{_value(a_value) if a_value is not None else "—"}</td>'
                        f'<td class="num">{_value(b_value) if b_value is not None else "—"}</td><td class="num">{delta}</td></tr>')
    return (
        f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>实验对比</title><style>{CSS}</style></head><body><main>'
        f'<p><a href="/">← 全部运行</a></p><h1>实验对比</h1><p>A：{esc(name_a)}（{fmt_time(meta_a.get("start"))}，{esc((meta_a.get("meta") or {}).get("intent", ""))}）<br>'
        f'B：{esc(name_b)}（{fmt_time(meta_b.get("start"))}，{esc((meta_b.get("meta") or {}).get("intent", ""))}）</p>'
        f'<table><tr><th>关节</th><th>指标</th><th>A</th><th>B</th><th>B − A</th></tr>{"".join(rows) or "<tr><td colspan=5>没有可对比的运动指标</td></tr>"}</table>'
        f'</main></body></html>'
    )
