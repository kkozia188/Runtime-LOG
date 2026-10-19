"""Self-contained archived waveform viewer, no CDN or browser plugins."""
import html
from pathlib import Path


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
<p><code>left_jointN/target_position</code> 与 <code>left_jointN/position</code> 是目标和实测位置（rad）；<code>velocity</code> 为 rad/s，<code>torque_permille</code> 为额定力矩的千分比，<code>period_us</code> 为控制周期 µs。网页的位置和速度已转换成度。</p>
</details></section><script>''' + script + "</script>"
