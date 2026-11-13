"""Shared, offline-safe presentation. No remote fonts, frameworks, or assets."""
from __future__ import annotations

import html
from pathlib import Path

CSS = Path(__file__).with_name('ui.css').read_text(encoding='utf-8')


def icon(name: str, size: int = 20) -> str:
    """Small consistent outline glyphs; no remote icon dependency."""
    paths = {
        'activity': '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
        'history': '<path d="M3 11a9 9 0 1 1 2.7 7M3 4v7h7M12 7v5l3 2"/>',
        'issues': '<path d="m12 3 10 17H2L12 3Z"/><path d="M12 9v4m0 3v.1"/>',
        'box': '<path d="M3 7 12 3l9 4v10l-9 4-9-4V7Zm0 0 9 5 9-5M12 12v9"/>',
        'arrow': '<path d="M5 12h14m-6-6 6 6-6 6"/>',
        'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
        'help': '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 0c0 2-2.5 2-2.5 4m0 3v.1"/>',
        'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    }
    return f'<svg class="ui-icon" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths[name]}</svg>'


def shell(active: str = 'runs') -> str:
    links = [('issues', '/#issues', '异常分析', 'activity'), ('runs', '/#history', '运行历史', 'history'),
             ('live', '/live', '实时状态', 'grid')]
    nav = ''.join(f'<a href="{url}" data-nav="{key}"' + (' aria-current="page"' if key == active else '')
                  + f'>{icon(glyph)}<span>{label}</span></a>' for key, url, label, glyph in links)
    return ('<a class="skip-link" href="#main-content">跳到主要内容</a>'
            '<header class="app-header"><a class="brand" href="/" aria-label="Runtime-LOG 首页">'
            '<span class="brand-mark">' + icon('activity', 24) + '</span>'
            '<span>Runtime<span class="brand-suffix"> / LOG</span></span></a>'
            f'<nav class="main-nav" aria-label="主导航">{nav}</nav>'
            '<div class="rail-bottom"><details class="product-help"><summary>' + icon('help') + '<span>使用说明</span></summary>'
            '<div><p>网页仅查看数据，不发送控制指令。</p><p>事件与波形按时间关联，触发信息不等于故障根因。</p><p>归档保留期限与磁盘空间见首页。运行报告需手动刷新。</p></div></details>'
            '<span class="rail-caption">机器人运行诊断</span></div></header>')


TABLE_SCRIPT = '''<script>(()=>{function wrap(){document.querySelectorAll('main table').forEach(table=>{
if(table.closest('.table-scroll,.analysis-log'))return;
const region=document.createElement('div');region.className='table-scroll';region.tabIndex=0;
region.setAttribute('role','region');region.setAttribute('aria-label','数据表，可横向滚动');
table.before(region);region.append(table);
})}wrap();new MutationObserver(wrap).observe(document.querySelector('main'),{childList:true,subtree:true})})();</script>'''


def frame_page(page: str, active: str = 'runs') -> str:
    """Give secondary self-contained reports the same navigation and table behavior."""
    if '<meta name="viewport"' not in page:
        page = page.replace('</head>', '<meta name="viewport" content="width=device-width,initial-scale=1"></head>')
    return page.replace('<body>', '<body>' + shell(active), 1).replace('<main>', '<main id="main-content">', 1).replace('</body>', TABLE_SCRIPT + '</body>')


def document(title: str, body: str, active: str = 'runs', script: str = '') -> str:
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape(title)} · Runtime-LOG</title><style>{CSS}</style></head>'
            f'<body>{shell(active)}<main id="main-content">{body}</main>{script}{TABLE_SCRIPT}</body></html>')


def json_script(value: object) -> str:
    """JSON safe inside a script element, including untrusted launch log text."""
    import json
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
