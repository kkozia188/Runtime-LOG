(() => {
  'use strict';
  const $ = id => document.getElementById('wave-' + id);
  const colors = ['#0969da', '#bc4c00'];
  const groups = [
    ['位置 / °', ['target_position', 'position']], ['跟随误差 / °', ['error']],
    ['速度', ['sent_velocity', 'velocity']], ['CSV 速度误差 / rad/s', ['velocity_error']],
    ['力矩 / ‰', ['torque_permille']], ['反馈年龄 / ms', ['feedback_age_ms']],
    ['控制周期 / µs', ['period_us']], ['驱动状态字', ['status_word']],
    ['实际模式（8=CSP，9=CSV）', ['mode_of_operation_display']], ['传感器就绪', ['ready']],
    ['适配器故障锁存', ['mode_request_error']], ['指令有效', ['command_fresh']]
  ];
  let data = null, charts = [], loading = false, cursor = null;
  const hidden = new Set(['status_word', 'mode_of_operation_display', 'mode_request_error', 'command_fresh']);
  const fmt = n => Number.isFinite(n) ? Number(n.toPrecision(7)).toString() : '—';
  const message = (text, error = false) => {
    $('message').textContent = text;
    $('message').dataset.error = String(error);
  };
  function bounds() {
    return [data.start, Math.max(data.end, data.start + 0.001)];
  }
  async function load(all = false) {
    if (loading || !$('joint').value) return;
    const start = all ? 0 : Number($('start').value);
    const end = all || !$('end').value ? null : Number($('end').value);
    if (!Number.isFinite(start) || start < 0 || (end !== null && (!Number.isFinite(end) || end <= start))) {
      message('请输入有效时间范围：终点必须大于起点。', true); return;
    }
    const query = new URLSearchParams({joint: $('joint').value, start, width: 1000});
    if (end !== null) query.set('end', end);
    loading = true;
    document.querySelectorAll('.wave-controls button,.wave-controls select,.wave-controls input').forEach(e => e.disabled = true);
    message('正在读取归档波形…');
    try {
      const response = await fetch('waveform.json?' + query);
      if (!response.ok) throw new Error(await response.text());
      data = await response.json(); cursor = null;
      $('start').value = data.start.toFixed(6); $('end').value = data.end.toFixed(6);
      message(data.samples ? `${data.samples.toLocaleString()} 个采样 · 此区间缺失 ${data.missing} 周期 · ` +
        (data.aggregated ? '峰谷摘要；光标显示保留采样值，继续放大可查看原始采样。' : '原始采样；光标显示最近采样点。') : '此时间范围没有采样。');
      build();
    } catch (error) {
      data = null; charts = []; $('charts').replaceChildren(); $('choices').replaceChildren();
      message('读取失败：' + error.message + '。可重试或下载 CSV。', true);
    } finally {
      loading = false;
      document.querySelectorAll('.wave-controls button,.wave-controls select,.wave-controls input').forEach(e => e.disabled = false);
    }
  }
  function selectRange(start, end) {
    if (!data || loading) return;
    const span = Math.min(data.duration, end - start);
    start = Math.max(0, Math.min(start, data.duration - span));
    $('start').value = start.toFixed(6); $('end').value = (start + span).toFixed(6);
    load();
  }
  function build() {
    charts = []; $('charts').replaceChildren(); $('choices').replaceChildren();
    for (const [groupTitle, keys] of groups) {
      const signals = data.series.filter(s => keys.includes(s.key));
      if (!signals.length) continue;
      const title = groupTitle === '速度' ? `速度 / ${signals[0].unit}` : groupTitle;
      const label = document.createElement('label'), checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.checked = !hidden.has(keys[0]);
      label.append(checkbox, ' ' + title); $('choices').append(label);
      const panel = document.createElement('div'); panel.className = 'wave-chart';
      const heading = document.createElement('h3'); heading.textContent = title;
      const canvas = document.createElement('canvas'); canvas.setAttribute('aria-label', title + '波形，拖拽放大；也可使用上方时间范围控件');
      const values = document.createElement('div'); values.className = 'wave-values';
      panel.append(heading, canvas, values); panel.hidden = !checkbox.checked; $('charts').append(panel);
      const chart = {panel, canvas, values, signals, drag: null}; charts.push(chart);
      checkbox.onchange = () => { panel.hidden = !checkbox.checked; checkbox.checked ? hidden.delete(keys[0]) : hidden.add(keys[0]); draw(chart); };
      const timeAt = event => {
        const rect = canvas.getBoundingClientRect(), [a, b] = bounds();
        return a + Math.max(0, Math.min(1, (event.clientX - rect.left - 76) / Math.max(1, rect.width - 96))) * (b - a);
      };
      canvas.onpointerdown = event => {
        if (loading) return;
        chart.drag = timeAt(event); canvas.setPointerCapture(event.pointerId);
      };
      canvas.onpointermove = event => {
        cursor = timeAt(event); charts.forEach(draw);
      };
      canvas.onpointerup = event => {
        if (chart.drag === null) return;
        const from = chart.drag, to = timeAt(event); chart.drag = null;
        const [a, b] = bounds();
        if (Math.abs(to - from) > (b - a) * 0.012) selectRange(Math.min(from, to), Math.max(from, to));
        else {cursor = to; charts.forEach(draw);}
      };
      canvas.onpointercancel = () => { chart.drag = null; draw(chart); };
    }
    charts.forEach(draw);
  }
  function draw(chart) {
    if (!data || chart.panel.hidden) return;
    const {canvas, signals, values} = chart;
    const w = canvas.clientWidth, h = 220, ratio = window.devicePixelRatio || 1;
    canvas.width = w * ratio; canvas.height = h * ratio;
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    const left = 76, right = Math.max(left + 1, w - 20), top = 20, bottom = h - 32;
    const [a, b] = bounds();
    let lo = Infinity, hi = -Infinity;
    for (const signal of signals) for (const [t, y] of signal.points) {
      if (y !== null && t >= a && t <= b) { lo = Math.min(lo, y); hi = Math.max(hi, y); }
    }
    if (!Number.isFinite(lo)) {lo = 0; hi = 1;}
    const pad = (hi - lo) * 0.08 || Math.max(Math.abs(lo) * 0.01, 0.001); lo -= pad; hi += pad;
    const x = t => left + (t - a) / (b - a) * (right - left);
    const y = v => bottom - (v - lo) / (hi - lo) * (bottom - top);
    ctx.font = '11px monospace'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yy = top + (bottom - top) * i / 4;
      ctx.strokeStyle = '#eaeef2'; ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(right, yy); ctx.stroke();
      ctx.fillStyle = '#57606a'; ctx.textAlign = 'right'; ctx.fillText(Number((hi - (hi - lo) * i / 4).toPrecision(4)).toString(), left - 7, yy + 4);
      if (w < 600 && i % 2) continue;
      ctx.textAlign = i === 0 ? 'left' : i === 4 ? 'right' : 'center'; const tt = a + (b - a) * i / 4;
      ctx.fillText(tt.toFixed(3) + ' s', x(tt), h - 10);
    }
    signals.forEach((signal, index) => {
      ctx.strokeStyle = colors[index % colors.length]; ctx.lineWidth = 1.4; ctx.beginPath(); let pen = false;
      for (const [t, v] of signal.points) {
        if (v === null) {pen = false; continue;}
        if (pen) ctx.lineTo(x(t), y(v)); else ctx.moveTo(x(t), y(v));
        pen = true;
      }
      ctx.stroke();
    });
    if (cursor !== null) {
      ctx.strokeStyle = '#57606a'; ctx.beginPath(); ctx.moveTo(x(cursor), top); ctx.lineTo(x(cursor), bottom); ctx.stroke();
      if (chart.drag !== null) {
        ctx.fillStyle = '#0969da22'; ctx.fillRect(x(chart.drag), top, x(cursor) - x(chart.drag), bottom - top);
      }
    }
    values.replaceChildren();
    signals.forEach((signal, index) => {
      const span = document.createElement('span'); span.style.color = colors[index % colors.length];
      let reading = '';
      if (cursor !== null && signal.points.length) {
        let l = 0, r = signal.points.length;
        while (l < r) {const mid = (l + r) >> 1; if (signal.points[mid][0] < cursor) l = mid + 1; else r = mid;}
        const p = signal.points[Math.min(l, signal.points.length - 1)];
        const before = signal.points[Math.max(0, l - 1)];
        const near = Math.abs(before[0] - cursor) < Math.abs(p[0] - cursor) ? before : p;
        reading = ` = ${near[1] === null ? '缺失' : fmt(near[1])} ${signal.unit} @ ${near[0].toFixed(6)} s`;
      }
      span.textContent = signal.label + reading + '　'; values.append(span);
    });
  }
  $('load').onclick = () => load(); $('all').onclick = () => load(true);
  $('joint').onchange = () => load();
  $('in').onclick = () => {if (data) {const [a,b] = bounds(); selectRange(a + (b-a)/4, b - (b-a)/4);}};
  $('prev').onclick = () => {if (data) {const [a,b] = bounds(); selectRange(a-(b-a), a);}};
  $('next').onclick = () => {if (data) {const [a,b] = bounds(); selectRange(b, b+(b-a));}};
  new ResizeObserver(() => charts.forEach(draw)).observe($('charts'));
  window.addEventListener('hashchange', () => {if (location.hash === '#waveforms' && !data) load(true);});
  if (location.hash === '#waveforms') load(true);
})();
