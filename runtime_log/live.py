"""Read-only live status from the recorder segments of running runs (latest cycle only)."""

from __future__ import annotations

import html
import math
import os
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
FORCE_SENSORS = {"left_wrist_force_sensor": "左臂", "right_wrist_force_sensor": "右臂"}
# EtherCAT protocol V1.1 (2026-09-10), TxPDO 0x6000:01..06.
# User-supplied DOCX SHA256: 9c275c21bb171a99cd814c2a041b8bc9b03774ef1034cac523104c1e63ab8cdf.
BLUEPOINT_RAW_PER_UNIT = 100_000
FORCE_UNITS = ["N", "N", "N", "N·m", "N·m", "N·m"]


def force_sensor_rows(segments: list[dict]) -> list[dict]:
    """Keep absent sensors visible; age describes the recorder, not sensor sample freshness."""
    rows = []
    for name, label in FORCE_SENSORS.items():
        candidates = [(s, sensor) for s in segments for sensor in s["force_sensors"] if sensor["sensor"] == name]
        if not candidates:
            rows.append({"sensor": name, "label": label, "status": "未接入采集", "raw": [None] * 6,
                         "converted": [None] * 6, "units": FORCE_UNITS})
            continue
        segment, sensor = min(candidates, key=lambda pair: pair[0]["age_s"])
        status = "周期记录更新"
        if segment["state"] != "active":
            status = "记录已停用"
        elif not segment["writer_alive"] or not 0 <= segment["age_s"] <= 1:
            status = "记录已过期"
        elif any(value is None for value in sensor["raw"]):
            status = "读数不完整"
        elif segment["bus"].get("link_up") == 0 or segment["bus"].get("al_states", {}).get(str(sensor["ring_position"])) not in (None, "OP"):
            status = "总线未就绪"
        rows.append({**sensor, "label": label, "status": status, "age_s": segment["age_s"]})
    return rows


def _num(value: float, digits: int = 4) -> Optional[float]:
    return None if value != value or math.isinf(value) else round(value, digits)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def snapshot(archive: Archive, enums: dict, shm_dir: Path = SHM_DIR) -> dict:
    runs = []
    for run in archive.runs():
        if run.get("result") != "running" or not run.get("control_pid"):
            continue
        pid = run["control_pid"]
        if not _process_alive(pid):
            continue
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
                                 "writer_alive": reader.writer_alive(),
                                 **decode(fields, enums)})
            finally:
                reader.close()
        runs.append({"id": run["id"], "scope": run.get("scope"), "domain": run.get("domain"), "segments": segments,
                     "force_sensors": force_sensor_rows(segments)})
    return {"time": time.time(), "runs": runs, "force_sensors": force_sensor_rows([]) if not runs else []}


def decode(fields: dict, enums: dict) -> dict:
    joints: dict = {}
    bus: dict = {}
    states: dict = {}
    values: dict = {}
    force_sensors: dict = {}
    for name, value in fields.items():
        prefix, _, interface = name.rpartition("/")
        if name in enums:
            key, label = enum_name(enums[name], value)
            states[name] = {"title": enums[name].get("title", name), "name": key, "label": label}
        elif prefix.startswith("ethercat_slave_") and interface == "al_state":
            bus.setdefault("al_states", {})[prefix.removeprefix("ethercat_slave_")] = AL_STATES.get(int(value), str(int(value))) if value == value else "?"
        elif prefix in ("ethercat_master", "ethercat_domain"):
            bus[interface] = _num(value)
        elif prefix in FORCE_SENSORS:
            force_sensors.setdefault(prefix, {})[interface] = _num(value, 0)
        elif prefix and interface:
            joints.setdefault(prefix, {})[interface] = value
        else:
            values[name] = _num(value)
    rows = []
    for joint, channels in sorted(joints.items()):
        row = {"joint": joint}
        if "status_word" in channels and channels["status_word"] == channels["status_word"]:
            info = cia402.decode(int(channels["status_word"]))
            row.update(state=info["label"], status_word=info["word"], power=info["voltage_enabled"], warning=info["warning"])
        for key in ("position", "target_position"):
            if key in channels:
                row[f"{key}_deg"] = _num(channels[key] * RAD2DEG)
        if "position" in channels and "target_position" in channels:
            row["error_deg"] = _num((channels["target_position"] - channels["position"]) * RAD2DEG, 5)
        if "velocity" in channels:
            row["velocity_deg_s"] = _num(channels["velocity"] * RAD2DEG, 3)
        if "torque_permille" in channels:
            row["torque_permille"] = _num(channels["torque_permille"], 1)
        if "error_code" in channels and channels["error_code"] == channels["error_code"]:
            row["error_code"] = f"0x{int(channels['error_code']):04X}"
        if "control_word_sent" in channels and channels["control_word_sent"] == channels["control_word_sent"]:
            row["control_word"] = f"0x{int(channels['control_word_sent']):04X}"
        rows.append(row)
    sensors = [{"sensor": name, "ring_position": 8 if name.startswith("left_") else 16,
                "raw": [channels.get(f"channel_{i}_raw") for i in range(1, 7)],
                "sample_counter": channels.get("sample_counter_raw"),
                "status_code_raw": channels.get("status_code_raw"),
                "temperature_raw": channels.get("temperature_raw")}
               for name, channels in force_sensors.items()]
    for sensor in sensors:
        # Missing/invalid channels stay missing, rather than appearing as zero force.
        sensor["converted"] = [
            None if value is None else round(value / BLUEPOINT_RAW_PER_UNIT, 5)
            for value in sensor["raw"]
        ]
        sensor["units"] = FORCE_UNITS
    return {"joints": rows, "bus": bus, "states": states, "values": values, "force_sensors": sensors}


PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>实时状态 · Runtime-LOG</title><style>__CSS__ td.bad{background:#ffebe9;font-weight:600} .tag{font-size:12px;color:#57606a}</style></head>
<body><main style="max-width:1500px"><p><a href="/">← 全部运行</a></p><h1>实时状态 <span class="tag" id="stamp"></span></h1>
<p class="k">只读：每 0.5 s 读取运行中的记录段的最新一个周期（不影响控制循环）。跟随误差 = 实际发出的目标 − 实际位置。</p>
<div id="runs">加载中…</div></main>
<script>
const f=(v,d=3)=>v===null||v===undefined?'-':(typeof v==='number'?v.toFixed(d):v);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function forceTable(sensors){
  return `<div class="card" style="overflow-x:auto"><h3>左右臂六维力传感器 · EtherCAT</h3>
    <p class="k">协议 V1.1 换算：原始值 ÷ 100000。上方为换算值，下方保留 raw；未做安装标定或重力补偿。状态码和温度为协议保留字段，采样是否推进请查看采样计数。</p>
    <table style="min-width:1050px;white-space:nowrap"><tr><th>手臂</th><th>采集状态</th>${['Fx (N)','Fy (N)','Fz (N)','Mx (N·m)','My (N·m)','Mz (N·m)'].map(x=>`<th>${x}</th>`).join('')}<th>采样计数 raw</th><th>状态码 raw</th><th>温度 raw（保留）</th><th>记录滞后 s</th></tr>`+
    sensors.map(s=>`<tr><td>${esc(s.label)}</td><td class="${s.status==='周期记录更新'?'':'bad'}">${esc(s.status)}</td>`+
      s.raw.map((v,i)=>`<td class="num"><strong>${f(s.converted?.[i],5)}</strong><div class="k">raw ${f(v,0)}</div></td>`).join('')+
      `<td class="num">${f(s.sample_counter,0)}</td><td class="num">${f(s.status_code_raw,0)}</td><td class="num">${f(s.temperature_raw,0)}</td><td class="num">${f(s.age_s,3)}</td></tr>`).join('')+'</table></div>';
}
function render(data){
  const root=document.getElementById('runs');
  document.getElementById('stamp').textContent=new Date(data.time*1000).toLocaleTimeString();
  if(!data.runs.length){root.innerHTML='<p>没有运行中的记录。</p>'+forceTable(data.force_sensors||[]);return;}
  root.innerHTML=data.runs.map(run=>{
    const segs=run.segments.map(s=>{
      const states=Object.values(s.states).map(x=>`<span class="badge" style="background:#0969da">${x.title}：${x.label}</span>`).join(' ');
      const joints=s.joints.length?`<table><tr><th>关节</th><th>驱动状态</th><th>状态字</th><th>动力电</th><th>位置 °</th><th>目标 °</th><th>跟随误差 °</th><th>速度 °/s</th><th>力矩 ‰</th><th>错误码</th><th>控制字</th></tr>`+
        s.joints.map(j=>`<tr><td>${j.joint}</td><td class="${j.state==='故障'||j.state==='快速停机'?'bad':''}">${f(j.state)}</td><td class="mono">${f(j.status_word)}</td>`+
        `<td>${j.power===undefined?'-':(j.power?'有':'无')}</td><td class="num">${f(j.position_deg)}</td><td class="num">${f(j.target_position_deg)}</td>`+
        `<td class="num">${f(j.error_deg,4)}</td><td class="num">${f(j.velocity_deg_s,2)}</td><td class="num">${f(j.torque_permille,0)}</td>`+
        `<td class="mono ${j.error_code&&j.error_code!=='0x0000'?'bad':''}">${f(j.error_code)}</td><td class="mono">${f(j.control_word)}</td></tr>`).join('')+'</table>':'';
      const bus=s.bus&&Object.keys(s.bus).length?`<p class="mono">WKC 错误 ${f(s.bus.wc_error_count,0)} · 过程数据年龄 ${f(s.bus.process_data_age_ms,3)} ms · 应答从站 ${f(s.bus.slaves_responding,0)} · 链路 ${s.bus.link_up===1?'UP':f(s.bus.link_up,0)} · AL ${Object.entries(s.bus.al_states||{}).map(([k,v])=>k+':'+v).join(' ')}</p>`:'';
      const vals=Object.entries(s.values||{}).map(([k,v])=>`${k}=${v===null?'-':v}`).join(' · ');
      return `<div class="card"><b>${s.writer}</b> <span class="tag">段 ${s.state} · 周期 #${s.seq} · 实际周期 ${s.period_us} µs · 数据滞后 ${f(s.age_s,3)} s</span><p>${states}</p>${vals?`<p class="mono k">${vals}</p>`:''}${bus}${joints}</div>`;
    }).join('');
    return `<h2><a href="/${run.id}/">${run.id}</a> <span class="tag">${run.scope} · 域 ${run.domain}</span></h2>${forceTable(run.force_sensors||[])}${segs||'<p class="k">该运行没有记录段。</p>'}`;
  }).join('');
}
async function tick(){try{const r=await fetch('/live.json',{cache:'no-store'});if(!r.ok)throw new Error(r.status);render(await r.json());}catch(e){document.getElementById('stamp').textContent='连接中断，读数已停止更新';document.getElementById('runs').textContent='实时数据暂不可用，请等待重新连接。';}setTimeout(tick,500);}
tick();
</script></body></html>"""


def page() -> str:
    return PAGE.replace("__CSS__", CSS)


def esc(value) -> str:
    return html.escape(str(value))
