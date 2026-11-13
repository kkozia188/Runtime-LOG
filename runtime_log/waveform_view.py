"""Self-contained archived waveform viewer, no CDN or browser plugins."""
import html
import math
from pathlib import Path
from .ui import frame_page


def render_blackbox(run_id: str, name: str, trigger: dict) -> str:
    from .report import CSS

    esc = html.escape
    channels = sorted({field.rsplit('/', 1)[0] for field in trigger.get('fields', []) if field.endswith('/position')})
    window = trigger.get('window') or {}
    start = window.get('start', 0)
    rows = ''.join(f'<tr><td>{(t.get("time_ns", 0) / 1e9 - start):.6f}</td>'
                   f'<td>{esc(t.get("reason", ""))}</td><td>{esc(t.get("detail", ""))}</td></tr>'
                   for t in trigger.get('triggers', []))
    triggers = trigger.get('triggers') or []
    focus = (triggers[0].get('time_ns', 0) / 1e9 - start) if triggers else None
    viewer = render_waveform(channels, focus_s=focus).replace('href="data.csv"', 'href="window.csv"') if channels else '<p>此窗口是控制器内部状态记录；请下载 CSV 查看。</p>'
    return frame_page(f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>黑匣子 {esc(name)}</title><style>{CSS}</style></head><body><main>'
            f'<div class="breadcrumb"><a href="../../">返回运行 {esc(run_id)}</a><span>/</span>黑匣子</div><h1>故障窗口 · {esc(name)}</h1>'
            f'<p>采样 {window.get("cycles", 0)} 条 · 缺失 {window.get("missing", 0)} 周期。触发时间相对窗口第一条采样。</p>'
            '<p><a href="window.csv">CSV</a> · <a href="window.mcap">MCAP</a> · <a href="kernel.log">内核日志</a> · '
            '<a href="timeline.json">时间线</a> · <a href="trigger.json">触发元数据</a></p>'
            '<table><tr><th>t / s</th><th>原因</th><th>详情</th></tr>' + rows + '</table>'
            + viewer + '</main></body></html>')


def render_waveform(joints: list[str], focus_s: float | None = None) -> str:
    # Only explicit "全程" requests may start an unbounded query.
    if focus_s is None or not math.isfinite(focus_s) or focus_s < 0:
        initial_start, initial_end = 0.0, 12.0
    else:
        initial_start, initial_end = max(0.0, focus_s - 10.0), focus_s + 2.0
    options = "".join(f'<option>{html.escape(joint)}</option>' for joint in joints)
    script = Path(__file__).with_name("waveform_view.js").read_text(encoding="utf-8")
    return '''<section id="waveforms" class="waveforms">
<style>
.waveforms {margin:28px 0;border-top:1px solid var(--line);padding-top:16px}
.wave-controls {display:flex;align-items:end;flex-wrap:wrap;gap:10px;margin:16px 0}
.wave-controls label {display:grid;gap:6px;font-size:14px;color:var(--muted)}
.wave-controls input {width:130px}
.wave-controls select {max-width:100%}
.wave-controls button {cursor:pointer;white-space:nowrap}
.wave-controls button:disabled {opacity:.5;cursor:wait}
.signal-picker{margin:10px 0 16px;font-size:14px}.signal-picker>summary{width:fit-content;color:var(--muted)}
.wave-choices {display:flex;flex-wrap:wrap;gap:12px 20px;margin:14px 0;font-size:14px}
.wave-choices label {display:flex;align-items:center;gap:7px}
.wave-chart {border:1px solid var(--line);border-radius:8px;margin:16px 0;background:white;overflow:hidden}
.wave-chart h3 {margin:16px 20px 0;font-size:16px}
.wave-chart canvas {display:block;width:100%;height:260px;touch-action:none;cursor:crosshair}
.wave-values {padding:0 20px 14px;font:14px/1.7 ui-monospace,monospace;min-height:28px;overflow-wrap:anywhere}
#wave-message {min-height:28px;font-size:14px;color:var(--muted)}
#wave-message[data-error=true] {color:#b42318}
@media(max-width:600px) {.wave-controls {gap:8px}.wave-controls input {width:85px}}
</style>
<h2>交互波形</h2>
<details class="standalone-help"><summary>波形读图说明</summary><p>拖拽放大，悬停读数。时间以第一条采样为零；缺失数据断线。故障后的采样需结合驱动状态判断。</p></details>
<div class="wave-controls">
<label><span class="sr-only">关节 / 通道</span><select id="wave-joint" aria-label="关节或通道">''' + options + '''</select></label>
<label>起点 / s<input id="wave-start" type="number" value="''' + f'{initial_start:.6f}' + '''" min="0" step="0.001"></label>
<label>终点 / s<input id="wave-end" type="number" value="''' + f'{initial_end:.6f}' + '''" min="0" step="0.001" placeholder="全程"></label>
<button id="wave-load" type="button">读取</button>
<button id="wave-all" type="button">全程</button>
<button id="wave-in" type="button">放大 2 倍</button>
<button id="wave-prev" type="button">← 前一段</button>
<button id="wave-next" type="button">后一段 →</button>
</div>
<div id="wave-message" role="status" aria-live="polite">选择通道后读取波形。</div>
<details class="signal-picker"><summary>选择信号</summary><div id="wave-choices" class="wave-choices"></div></details><div id="wave-charts"></div>
<details><summary>用 PlotJuggler 查看原始采样</summary>
<p>下载本页 <a href="data.csv">CSV</a>。在 PlotJuggler 的 Data Loader 中打开 CSV，分隔符选逗号，时间列选 <code>time_s</code>（秒）。将左侧信号拖入图中。</p>
<p><code>target_position</code> 与 <code>position</code> 是目标和实测位置（CSV 中为 rad）；<code>velocity</code>、<code>sent_velocity</code> 为 rad/s，<code>torque_permille</code> 为额定力矩的千分比，<code>period_us</code> 为控制周期 µs。网页的位置使用度；含 sent_velocity 的底盘通道使用 rad/s，其他速度使用度/秒。请以各图单位为准。</p>
<p>底盘 CSV 速度误差仅显示模式9、运行使能、反馈新鲜且适配器有效的采样；失能或切换到模式8时显示断线。转向轴没有实际下发的位置镜像，因此不计算转向跟随误差。编码器以自身采样频率更新，1 kHz 记录可能重复旧值，请结合反馈年龄查看。</p>
</details></section><script>''' + script + "</script>"
