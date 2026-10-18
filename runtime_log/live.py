"""Read-only live status from the recorder segments of running runs (latest cycle only)."""

from __future__ import annotations

import html
import math
import time
from pathlib import Path
from typing import Optional

from . import cia402
from .archive import Archive
from .blackbox import enum_name
from .report import CSS
from .shm import SHM_DIR, ShmReader

RAD2DEG = 180.0 / math.pi
AL_STATES = {1: "INIT", 2: "PREOP", 3: "BOOT", 4: "SAFEOP", 8: "OP"}


def _num(value: float, digits: int = 4) -> Optional[float]:
    return None if value != value or math.isinf(value) else round(value, digits)


def snapshot(archive: Archive, enums: dict, shm_dir: Path = SHM_DIR) -> dict:
    runs = []
    for run in archive.runs():
        if run.get("result") != "running" or not run.get("control_pid"):
            continue
        pid = run["control_pid"]
        segments = []
        try:
            paths = sorted(p for p in shm_dir.iterdir() if p.name.startswith(f"runtime-log.{pid}."))
        except FileNotFoundError:
            paths = []
        for path in paths:
            try:
                reader = ShmReader(path)
            except (OSError, ValueError):
                continue
            try:
                latest = reader.latest()
                if latest is None:
                    continue
                seq, time_ns, period_ns, values = latest
                fields = dict(zip(reader.fields, values))
                segments.append({"writer": reader.writer_name, "state": reader.writer_state, "seq": seq,
                                 "age_s": round(time.time() - time_ns / 1e9, 3), "period_us": round(period_ns / 1e3, 1),
                                 **decode(fields, enums)})
            finally:
                reader.close()
        runs.append({"id": run["id"], "scope": run.get("scope"), "domain": run.get("domain"), "segments": segments})
    return {"time": time.time(), "runs": runs}


def decode(fields: dict, enums: dict) -> dict:
    joints: dict = {}
    bus: dict = {}
    states: dict = {}
    for name, value in fields.items():
        prefix, _, interface = name.rpartition("/")
        if name in enums:
            key, label = enum_name(enums[name], value)
            states[name] = {"title": enums[name].get("title", name), "name": key, "label": label}
        elif prefix.startswith("ethercat_slave_") and interface == "al_state":
            bus.setdefault("al_states", {})[prefix.removeprefix("ethercat_slave_")] = AL_STATES.get(int(value), str(int(value))) if value == value else "?"
        elif prefix in ("ethercat_master", "ethercat_domain"):
            bus[interface] = _num(value)
        elif prefix and interface:
            joints.setdefault(prefix, {})[interface] = value
        else:
            states[name] = {"title": name, "name": _num(value), "label": str(_num(value))}
    rows = []
    for joint, values in sorted(joints.items()):
        row = {"joint": joint}
        if "status_word" in values and values["status_word"] == values["status_word"]:
            info = cia402.decode(int(values["status_word"]))
            row.update(state=info["label"], status_word=info["word"], power=info["voltage_enabled"], warning=info["warning"])
        for key in ("position", "target_position"):
            if key in values:
                row[f"{key}_deg"] = _num(values[key] * RAD2DEG)
        if "position" in values and "target_position" in values:
            row["error_deg"] = _num((values["target_position"] - values["position"]) * RAD2DEG, 5)
        if "velocity" in values:
            row["velocity_deg_s"] = _num(values["velocity"] * RAD2DEG, 3)
        if "torque_permille" in values:
            row["torque_permille"] = _num(values["torque_permille"], 1)
        if "error_code" in values and values["error_code"] == values["error_code"]:
            row["error_code"] = f"0x{int(values['error_code']):04X}"
        if "control_word_sent" in values and values["control_word_sent"] == values["control_word_sent"]:
            row["control_word"] = f"0x{int(values['control_word_sent']):04X}"
        rows.append(row)
    return {"joints": rows, "bus": bus, "states": states}


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>实时状态 · Runtime-LOG</title><style>__CSS__ td.bad{background:#ffebe9;font-weight:600} .tag{font-size:12px;color:#57606a}</style></head>
<body><main style="max-width:1500px"><p><a href="/">← 全部运行</a></p><h1>实时状态 <span class="tag" id="stamp"></span></h1>
<p class="k">只读：每 0.5 s 读取运行中的记录段的最新一个周期（不影响控制循环）。跟随误差 = 实际发出的目标 − 实际位置。</p>
<div id="runs">加载中…</div></main>
<script>
const f=(v,d=3)=>v===null||v===undefined?'-':(typeof v==='number'?v.toFixed(d):v);
function render(data){
  const root=document.getElementById('runs');
  document.getElementById('stamp').textContent=new Date(data.time*1000).toLocaleTimeString();
  if(!data.runs.length){root.innerHTML='<p>没有运行中的记录。</p>';return;}
  root.innerHTML=data.runs.map(run=>{
    const segs=run.segments.map(s=>{
      const states=Object.values(s.states).map(x=>`<span class="badge" style="background:#0969da">${x.title}：${x.label}</span>`).join(' ');
      const joints=s.joints.length?`<table><tr><th>关节</th><th>驱动状态</th><th>状态字</th><th>动力电</th><th>位置 °</th><th>目标 °</th><th>跟随误差 °</th><th>速度 °/s</th><th>力矩 ‰</th><th>错误码</th><th>控制字</th></tr>`+
        s.joints.map(j=>`<tr><td>${j.joint}</td><td class="${j.state==='故障'||j.state==='快速停机'?'bad':''}">${f(j.state)}</td><td class="mono">${f(j.status_word)}</td>`+
        `<td>${j.power===undefined?'-':(j.power?'有':'无')}</td><td class="num">${f(j.position_deg)}</td><td class="num">${f(j.target_position_deg)}</td>`+
        `<td class="num">${f(j.error_deg,4)}</td><td class="num">${f(j.velocity_deg_s,2)}</td><td class="num">${f(j.torque_permille,0)}</td>`+
        `<td class="mono ${j.error_code&&j.error_code!=='0x0000'?'bad':''}">${f(j.error_code)}</td><td class="mono">${f(j.control_word)}</td></tr>`).join('')+'</table>':'';
      const bus=s.bus&&Object.keys(s.bus).length?`<p class="mono">WKC 错误 ${f(s.bus.wc_error_count,0)} · 过程数据年龄 ${f(s.bus.process_data_age_ms,3)} ms · 应答从站 ${f(s.bus.slaves_responding,0)} · 链路 ${s.bus.link_up===1?'UP':f(s.bus.link_up,0)} · AL ${Object.entries(s.bus.al_states||{}).map(([k,v])=>k+':'+v).join(' ')}</p>`:'';
      return `<div class="card"><b>${s.writer}</b> <span class="tag">段 ${s.state} · 周期 #${s.seq} · 实际周期 ${s.period_us} µs · 数据滞后 ${f(s.age_s,3)} s</span><p>${states}</p>${bus}${joints}</div>`;
    }).join('');
    return `<h2><a href="/${run.id}/">${run.id}</a> <span class="tag">${run.scope} · 域 ${run.domain}</span></h2>${segs||'<p class="k">该运行没有记录段。</p>'}`;
  }).join('');
}
async function tick(){try{const r=await fetch('/live.json',{cache:'no-store'});render(await r.json());}catch(e){}setTimeout(tick,500);}
tick();
</script></body></html>"""


def page() -> str:
    return PAGE.replace("__CSS__", CSS)


def esc(value) -> str:
    return html.escape(str(value))
