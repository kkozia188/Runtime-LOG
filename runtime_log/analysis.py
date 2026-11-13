"""Read-only, run-relative evidence workbench. Metadata only until a waveform is requested."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from .archive import read_json
from .model import Event
from .ui import json_script
from .waveform_view import render_waveform


def evidence_of(run_dir: Path | None, t0: float) -> list[dict]:
    if run_dir is None:
        return []
    evidence = []
    for kind, filename, datafile in [('blackbox', 'trigger.json', 'window.mcap'), ('experiments', 'meta.json', 'data.mcap')]:
        for path in sorted((run_dir / kind).glob(f'*/{filename}')):
            meta = read_json(path, {})
            if not isinstance(meta, dict) or not (path.parent / datafile).is_file():
                continue
            if kind == 'blackbox':
                window = meta.get('window') or {}
                start, end = window.get('start'), window.get('end')
                channels = sorted({f.rsplit('/', 1)[0] for f in meta.get('fields', []) if f.endswith('/position')})
                label = '故障窗口 · ' + path.parent.name
            else:
                start, end = meta.get('start'), meta.get('end')
                if end is None and start is not None:
                    end = start + (meta.get('duration_s') or 0)
                channels = meta.get('waveform_channels') or sorted(meta.get('joints') or {})
                label = '数据记录 · ' + (meta.get('name') or path.parent.name)
            if not channels or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            # Metadata is used only for candidate selection; MCAP timestamps determine actual alignment.
            base = f'{kind}/{quote(path.parent.name, safe="")}/'
            evidence.append({'label': label, 'kind': kind, 'start': start - t0, 'end': end - t0,
                             'channels': channels, 'endpoint': base + 'waveform.json', 'href': base,
                             'csv': base + ('window.csv' if kind == 'blackbox' else 'data.csv')})
    return evidence


def analysis_data(run: dict, events: list[Event], run_dir: Path | None) -> dict:
    summary = run.get('summary') or {}
    t0 = summary.get('t0') or run.get('start') or 0
    rows = []
    for event in sorted(events, key=lambda e: e.ts):
        expected = event.rule == 'blackbox_expected'
        issue = not expected and (event.level in ('warn', 'error') or event.kind == 'failure' or event.rule == 'blackbox_saved')
        rows.append({'t': round(event.ts - t0, 6), 'level': event.level, 'kind': event.kind,
                     'source': event.source, 'subject': event.subject, 'label': event.label or event.rule or event.kind,
                     'message': event.message, 'issue': issue, 'expected': expected})
    evidence = evidence_of(run_dir, t0)
    duration = max([1, summary.get('duration_s') or 0] + [r['t'] for r in rows] + [e['end'] for e in evidence])
    return {'t0': t0, 'duration': duration, 'events': rows, 'evidence': evidence}


def render_analysis(run: dict, events: list[Event], run_dir: Path | None) -> str:
    data = analysis_data(run, events, run_dir)
    script = Path(__file__).with_name('analysis.js').read_text(encoding='utf-8')
    return '''<section id="analysis" aria-label="故障分析工作台">
<noscript><p class="notice warn">联动分析需要 JavaScript。下方完整报告仍可阅读与下载。</p></noscript>
<div class="analysis-layout">
<aside class="issue-sidebar"><header><h2>事件 <span class="count" id="issue-count">0</span></h2></header>
<div id="issue-list" class="issue-scroll"></div><div class="pager" style="padding:0 16px"><button id="issue-more" type="button" hidden>加载更多异常</button></div></aside>
<div class="analysis-main"><section class="panel"><div class="selection-title"><span id="selected-level" class="badge neutral">运行概览</span><h2 id="selected-title">事件与证据</h2></div>
<div class="status-line"><span id="selected-time"></span><span id="selected-subject"></span></div>
<p id="selected-message" class="selection-message"></p>
<div class="timeline-track" id="analysis-timeline" aria-label="异常时间线"></div><div class="timeline-scale"><span>0 s</span><span id="duration-label"></span></div>
<details class="time-scrub"><summary>定位时间</summary><input id="time-cursor" type="range" min="0" step="0.001" value="0" style="width:100%" aria-label="定位运行时间（秒）"></details>
<div class="range-toolbar"><label><span class="sr-only">起点 / s</span><input id="range-start" aria-label="范围起点（秒）" type="number" min="0" step="0.001" value="0"></label><span class="k">—</span><label><span class="sr-only">终点 / s</span><input id="range-end" aria-label="范围终点（秒）" type="number" min="0" step="0.001"></label><span class="k">s</span>
<button id="range-apply" class="primary" type="button">应用</button><button id="range-reset" type="button">故障前后</button><button id="range-all" type="button">全程</button></div><p id="range-status" class="range-status" role="status"></p></section>
<section class="panel"><div class="panel-head"><h2>波形</h2><a id="evidence-detail" class="button" hidden>完整记录</a></div>
<div class="evidence-toolbar"><label><span class="sr-only">归档数据</span><select id="evidence-select" aria-label="选择归档波形"><option value="">自动匹配归档</option></select></label><a id="evidence-csv" class="button" hidden>下载 CSV</a></div>
<p id="evidence-empty" class="notice" role="status">请选择一个事件或时间范围。</p><div id="embedded-wave" hidden>''' + render_waveform([]) + '''</div></section>
<section class="panel"><div class="panel-head"><h2>时段日志</h2><span class="count" id="log-count">0</span></div>
<div class="log-controls"><input id="log-search" type="search" aria-label="搜索时段日志" placeholder="搜索日志"><label><input id="log-issues" type="checkbox"> 仅异常</label></div>
<div class="analysis-log" tabindex="0" role="region" aria-label="时段日志，可滚动"><table><thead><tr><th>时间 / s</th><th>级别</th><th>来源</th><th>事件 / 原文</th></tr></thead><tbody id="log-rows"></tbody></table></div>
<p id="log-empty" class="empty-state" hidden>没有匹配的日志。尝试扩大范围或清空筛选。</p>
<div class="pager"><span id="log-page" class="k"></span><div><button id="log-prev" type="button">上一页</button> <button id="log-next" type="button">下一页</button></div></div></section>
</div></div></section><script id="analysis-data" type="application/json">''' + json_script(data) + '</script><script>' + script + '</script>'
