"""Fault-first archive index with a quiet workspace shell and progressive detail."""
from __future__ import annotations

from pathlib import Path
from .report import esc, fmt_s, fmt_time, RESULT_LABEL
from .timeline import is_anomaly_key_informational
from .ui import document, icon, json_script


def actionable(run: dict) -> int:
    return sum(v for k, v in (run.get('anomalies') or {}).items() if not is_anomaly_key_informational(k))


def status(run: dict) -> str:
    result = run.get('result', '')
    tone = {'failed': 'danger', 'aborted': 'warning', 'interrupted': 'warning', 'running': 'soft', 'ok': 'success'}.get(result, 'neutral')
    return f'<span class="badge {tone}">{esc(RESULT_LABEL.get(result, result or "未知"))}</span>'


def scope_name(run: dict) -> str:
    return {'chassis_only': '底盘', 'arms_only': '机械臂'}.get(run.get('scope'), run.get('scope') or '未标注设备')


def render_home(index: dict, station: dict | None = None) -> str:
    station = station or {}
    runs = index.get('runs') or []
    usage = index.get('usage') or {}
    current = max(runs, key=lambda r: r.get('start') or 0, default=None)
    current_runs = [current] if current else []
    issues = [r for r in current_runs if r.get('result') in ('failed', 'aborted', 'interrupted') or actionable(r) or r.get('blackbox_saved')]
    running = sum(r.get('result') == 'running' for r in runs)
    incidents = []
    for run in issues:
        name = run.get('failure_label') or run.get('failed_stage')
        if not name:
            name = {'aborted': '运行未正常结束', 'interrupted': '记录中断'}.get(run.get('result'))
        if not name:
            name = '触发窗口待分析' if run.get('blackbox_saved') else '运行中出现异常事件'
        incidents.append(f'<article class="incident-row"><div class="incident-symbol">{icon("issues", 22)}</div><div class="incident-content">'
                         f'<a class="incident-title" href="/{esc(run["id"])}/#analysis">{esc(name)}</a>'
                         f'<div class="incident-meta"><span>{esc(scope_name(run))}</span><span>{fmt_time(run.get("start"))}</span>'
                         f'<span>{run.get("blackbox_saved") or 0} 个窗口</span></div></div>'
                         f'<div>{status(run)}</div><a class="button incident-action" href="/{esc(run["id"])}/#analysis">分析 {icon("arrow", 17)}</a></article>')
    issue_body = '<div class="incident-list">' + ''.join(incidents) + '</div>' if incidents else (
        '<div class="empty-state"><h3>本次运行暂无异常记录</h3></div>'
        if current else '<div class="empty-state"><h3>等待第一份运行记录</h3><p>发现控制栈运行后，记录会自动出现在这里。</p></div>')
    current_header = (f'<div class="panel-head"><div><h2>{esc(scope_name(current))}</h2>'
                      f'<p class="k">{fmt_time(current.get("start"))} · {fmt_s(current.get("duration_s"))}</p></div>'
                      f'<div class="heading-actions">{status(current)} <a class="button" href="/{esc(current["id"])}/">打开本次运行 {icon("arrow",17)}</a></div></div>') if current else ''
    rows = []
    for run in runs:
        count = actionable(run)
        rows.append(f'<tr data-result="{esc(run.get("result"))}" data-hw="{esc(run.get("hardware"))}" data-issues="{int(bool(count or run.get("blackbox_saved") or run.get("result") in ("failed", "aborted", "interrupted")))}">'
                    f'<td><input type="checkbox" class="pick" aria-label="选择运行 {esc(run["id"])}" value="{esc(run["id"])}"></td>'
                    f'<td class="run-id"><a class="run-link" href="/{esc(run["id"])}/">{esc(scope_name(run))}</a>'
                    f'<div class="k">{fmt_time(run.get("start"))}</div><span class="run-search-only" hidden>{esc(run["id"])} {esc(run.get("scope"))}</span></td>'
                    f'<td>{status(run)}</td><td class="num">{fmt_s(run.get("duration_s"))}</td>'
                    f'<td class="num">{count if count else "—"}</td>'
                    f'<td><span class="data-count">{run.get("blackbox_saved") or 0} 窗口</span><span class="data-count">{run.get("experiments") or 0} 记录</span></td>'
                    f'<td>{esc(run.get("hardware") or "—")}</td>'
                    f'<td class="extra-col">{esc(run.get("domain"))}</td><td class="extra-col num">{fmt_s(run.get("ready_s"))}</td>'
                    f'<td class="extra-col">{esc(run.get("failed_stage") or "—")}<div class="k">{esc(run.get("failure_label"))}</div></td>'
                    f'<td class="extra-col">{esc(run.get("last_stage") or "—")}</td>'
                    f'<td class="extra-col num">{esc(run.get("peak_cpu") if run.get("peak_cpu") is not None else "—")}</td>'
                    f'<td class="extra-col">{esc(run.get("source") or "—")}<div class="mono">{esc(run["id"])}</div></td>'
                    f'<td><a class="row-open" href="/{esc(run["id"])}/" aria-label="打开运行 {esc(run["id"])}">{icon("arrow",18)}</a></td></tr>')
    peers = [p for p in station.get('peers') or [] if p.get('url', '').startswith(('https://', 'http://'))]
    links = '<div class="peer-links">其他工控机：' + ' '.join(f'<a href="{esc(p["url"])}">{esc(p.get("name") or p["url"])} →</a>' for p in peers) + '</div>' if peers else ''
    free = f'{usage["disk_free_bytes"] / 1024**3:.1f} GiB' if usage.get('disk_free_bytes') is not None else '未知'
    archive = f'{usage["archive_bytes"] / 1024**2:.1f} MiB' if usage.get('archive_bytes') is not None else '未知'
    body = f'''<div class="workspace-top"><span>{esc(station.get('name') or '本机记录站')}</span><span class="workspace-state">{running} 次运行中</span></div>
<div class="page-heading"><h1><span id="home-title">本次运行</span><span class="heading-period">.</span></h1><div class="heading-actions"><a class="button" href="/live">{icon('activity',18)} 实时状态</a></div></div>
<div id="index-update" class="notice" hidden role="status">运行历史已更新。<button id="refresh-index" type="button">刷新列表</button></div>
<div id="current-run">
{current_header}
<div class="overview-band"><div class="overview-number"><strong>{actionable(current) if current else 0}</strong><span>异常计数</span></div><div class="overview-divider"></div><div class="overview-secondary"><strong>{(current or {}).get('blackbox_saved') or 0}</strong><span>触发窗口</span></div><div class="overview-secondary"><strong>{(current or {}).get('experiments') or 0}</strong><span>数据记录</span></div><div class="overview-storage">磁盘剩余<strong>{free}</strong></div></div>{links}
<section id="issues"><div class="panel-head"><h2>本次异常</h2></div>{issue_body}</section>
</div>
<section id="history" class="history-section" hidden><div class="history-heading"><h2>全部运行 <span class="count" id="visible-count">{len(runs)}</span></h2><button id="toggle-columns" class="button" type="button" aria-pressed="false">详细字段</button></div>
<div class="filters"><label><span class="sr-only">结果</span><select id="fResult" aria-label="运行结果"><option value="">全部结果</option><option value="failed">失败</option><option value="ok">正常结束</option><option value="running">运行中</option><option value="aborted">未正常结束</option><option value="interrupted">记录中断</option></select></label>
<label><span class="sr-only">硬件</span><select id="fHw" aria-label="硬件类型"><option value="">全部硬件</option><option>ethercat</option><option>mock</option></select></label><label><input id="fAnomaly" type="checkbox"> 仅异常</label><label class="search-field">{icon('search',18)}<input id="fText" type="search" aria-label="搜索运行" placeholder="搜索运行或报错"></label></div>
<div class="table-scroll" tabindex="0" role="region" aria-label="运行历史，可横向滚动"><table class="run-table" id="runs"><thead><tr><th>选择</th><th>设备 / 开始时间</th><th>结果</th><th>时长</th><th>异常</th><th>数据</th><th>硬件</th><th class="extra-col">域</th><th class="extra-col">就绪耗时</th><th class="extra-col">失败步骤</th><th class="extra-col">最后成功步骤</th><th class="extra-col">峰值 CPU%</th><th class="extra-col">来源 / ID</th><th></th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p id="filter-empty" class="empty-state" {'hidden' if runs else ''}>没有符合条件的运行。</p>
<div class="compare-toolbar"><button id="compareBtn" type="button" disabled>对比所选运行</button><span id="picked-note" class="k"></span></div></section>
<footer class="footer"><span>索引更新 {fmt_time(index.get('generated'))}</span><span>归档 {archive} · 保留 7 天</span></footer>'''
    data = json_script({'generated': index.get('generated')})
    script = Path(__file__).with_name('index_view.js').read_text(encoding='utf-8')
    return document('异常分析', body, 'issues', f'<script id="index-state" type="application/json">{data}</script><script>{script}</script>')
