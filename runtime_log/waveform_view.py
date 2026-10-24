"""Self-contained archived waveform viewer, no CDN or browser plugins."""
import html
from pathlib import Path


def render_blackbox(run_id: str, name: str, trigger: dict) -> str:
    from .report import CSS

    esc = html.escape
    channels = sorted({field.rsplit('/', 1)[0] for field in trigger.get('fields', []) if field.endswith('/position')})
    window = trigger.get('window') or {}
    start = window.get('start', 0)
    rows = ''.join(f'<tr><td>{(t.get("time_ns", 0) / 1e9 - start):.6f}</td>'
                   f'<td>{esc(t.get("reason", ""))}</td><td>{esc(t.get("detail", ""))}</td></tr>'
                   for t in trigger.get('triggers', []))
    viewer = render_waveform(channels).replace('href="data.csv"', 'href="window.csv"') if channels else '<p>此窗口是控制器内部状态记录；请下载 CSV 查看。</p>'
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>黑匣子 {esc(name)}</title><style>{CSS}</style></head><body><main>'
            f'<p><a href="../../">← 运行 {esc(run_id)}</a></p><h1>黑匣子 {esc(name)}</h1>'
            f'<p>采样 {window.get("cycles", 0)} 条 · 缺失 {window.get("missing", 0)} 周期。触发时间相对窗口第一条采样。</p>'
            '<p><a href="window.csv">CSV</a> · <a href="window.mcap">MCAP</a> · <a href="kernel.log">内核日志</a> · '
            '<a href="timeline.json">时间线</a> · <a href="trigger.json">触发元数据</a></p>'
            '<table><tr><th>t / s</th><th>原因</th><th>详情</th></tr>' + rows + '</table>'
            + viewer + '</main></body></html>')


def render_waveform(joints: list[str]) -> str:
    options = "".join(f'<option>{html.escape(joint)}</option>' for joint in joints)
    script = Path(__file__).with_name("waveform_view.js").read_text(encoding="utf-8")
    return '''<section id="waveforms" class="waveforms">
<style>
.waveforms {margin:28px 0;border-top:1px solid #d0d7de;padding-top:12px}
.wave-controls {display:flex;align-items:end;flex-wrap:wrap;gap:12px;margin:12px 0}
.wave-controls label {display:grid;gap:4px;font-size:13px}
.wave-controls input {width:110px}
.wave-controls input,.wave-controls select,.wave-controls button {padding:7px;border:1px solid #8c959f;border-radius:4px;background:#fff;color:#24292f;font:inherit}
.wave-controls button {cursor:pointer;white-space:nowrap}
.wave-controls button:disabled {opacity:.5;cursor:wait}
.wave-controls :focus-visible {outline:2px solid #0969da;outline-offset:2px}
.wave-choices {display:flex;flex-wrap:wrap;gap:16px;margin:12px 0}
.wave-chart {border:1px solid #d0d7de;margin:12px 0;background:white;overflow:hidden}
.wave-chart h3 {margin:12px 16px 0;font-size:14px}
.wave-chart canvas {display:block;width:100%;height:220px;touch-action:none;cursor:crosshair}
.wave-values {padding:0 16px 10px;font:12px ui-monospace,monospace;min-height:20px;overflow-wrap:anywhere}
#wave-message {min-height:24px}
#wave-message[data-error=true] {color:#b42318}
@media(max-width:600px) {.wave-controls {gap:8px}.wave-controls input {width:85px}}
</style>
<h2>交互波形</h2>
<p class="k">拖拽图中时间区间放大；悬停或点击读数。各图共用时间轴，缺失数据会断线。时间以第一个采样点为零。波形包含故障后的归档数据，请结合驱动状态字判断。</p>
<div class="wave-controls">
<label>关节<select id="wave-joint">''' + options + '''</select></label>
<label>起点 / s<input id="wave-start" type="number" value="0" min="0" step="0.001"></label>
<label>终点 / s<input id="wave-end" type="number" min="0" step="0.001" placeholder="全程"></label>
<button id="wave-load" type="button">查看波形</button>
<button id="wave-all" type="button">全程</button>
<button id="wave-in" type="button">放大 2 倍</button>
<button id="wave-prev" type="button">← 前一段</button>
<button id="wave-next" type="button">后一段 →</button>
</div>
<div id="wave-message" role="status" aria-live="polite">按需读取已归档数据，不影响控制指令。</div>
<div id="wave-choices" class="wave-choices"></div><div id="wave-charts"></div>
<details><summary>用 PlotJuggler 查看原始采样</summary>
<p>下载本页 <a href="data.csv">CSV</a>。在 PlotJuggler 的 Data Loader 中打开 CSV，分隔符选逗号，时间列选 <code>time_s</code>（秒）。将左侧信号拖入图中。</p>
<p><code>target_position</code> 与 <code>position</code> 是目标和实测位置（CSV 中为 rad）；<code>velocity</code>、<code>sent_velocity</code> 为 rad/s，<code>torque_permille</code> 为额定力矩的千分比，<code>period_us</code> 为控制周期 µs。网页的位置使用度；含 sent_velocity 的底盘通道使用 rad/s，其他速度使用度/秒。请以各图单位为准。</p>
<p>底盘 CSV 速度误差仅显示模式9、运行使能、反馈新鲜且适配器有效的采样；失能或切换到模式8时显示断线。转向轴没有实际下发的位置镜像，因此不计算转向跟随误差。编码器以自身采样频率更新，1 kHz 记录可能重复旧值，请结合反馈年龄查看。</p>
</details></section><script>''' + script + "</script>"
