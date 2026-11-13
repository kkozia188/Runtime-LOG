(() => {
  'use strict';
  const $ = id => document.getElementById('wave-' + id);
  const colors = ['#4366d3', '#b7653d'];
  const groups = [
    ['位置 / °', ['target_position', 'position']], ['跟随误差 / °', ['error']],
    ['速度', ['sent_velocity', 'velocity']], ['速度误差 / rad/s', ['velocity_error']],
    ['力矩 / ‰', ['torque_permille']], ['反馈年龄 / ms', ['feedback_age_ms']],
    ['控制周期 / µs', ['period_us']], ['驱动状态字', ['status_word']],
    ['实际模式（8=CSP，9=CSV）', ['mode_of_operation_display']], ['传感器就绪', ['ready']],
    ['适配器故障锁存', ['mode_request_error']], ['指令有效', ['command_fresh']]
  ];
  let data = null, charts = [], loading = false, cursor = null;
  let endpoint = 'waveform.json', origin = null, generation = 0, controller = null, marker = null, preset = '';
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
    if (!$('joint').value) return;
    const start = all ? 0 : Number($('start').value);
    const end = all || !$('end').value ? null : Number($('end').value);
    if (!Number.isFinite(start) || start < 0 || (end !== null && (!Number.isFinite(end) || end <= start))) {
      message('请输入有效时间范围：终点必须大于起点。', true); return;
    }
    const query = new URLSearchParams({joint: $('joint').value, start, width: 1000});
    if (end !== null) query.set('end', end);
    if (origin !== null) query.set('origin', origin);
    const request = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    loading = true;
    document.querySelectorAll('.wave-controls button,.wave-controls select,.wave-controls input').forEach(e => e.disabled = true);
    message('正在读取归档波形…');
    try {
      let response;
      // A navigated-away request may still hold the server's single-reader lock.
      // Brief bounded retries avoid making the next page look broken; never flood the IPC.
      for (let attempt = 0; attempt < 4; attempt++) {
        response = await fetch(endpoint + '?' + query, {signal: controller.signal});
        if (response.status !== 503 || attempt === 3) break;
        message('其他归档正在读取，稍后自动重试…');
        await new Promise(resolve => setTimeout(resolve, 300 * (attempt + 1)));
        if (request !== generation) return;
      }
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      if (request !== generation) return;
      data = result; cursor = null;
      $('start').value = data.start.toFixed(6); $('end').value = data.end.toFixed(6);
      message(data.samples ? `${data.samples.toLocaleString()} 个采样${data.missing ? ` · 缺失 ${data.missing} 周期` : ''} · ` +
        (data.aggregated ? '峰谷摘要' : '原始采样') +
        (origin !== null ? ` · 实际覆盖运行 ${data.start.toFixed(3)}–${data.end.toFixed(3)} s` : '') : '此时间范围没有采样。');
      build();
    } catch (error) {
      if (request !== generation || error.name === 'AbortError') return;
      data = null; charts = []; $('charts').replaceChildren(); $('choices').replaceChildren();
      message('读取失败：' + error.message + '。可重试或下载 CSV。', true);
    } finally {
      if (request === generation) {
        loading = false;
        document.querySelectorAll('.wave-controls button,.wave-controls select,.wave-controls input').forEach(e => e.disabled = false);
      }
    }
  }
  function selectRange(start, end) {
    if (!data || loading) return;
    const first = Math.max(0, data.available_start || 0);
    const span = Math.min(data.duration - first, end - start);
    if (span <= 0) return;
    start = Math.max(first, Math.min(start, data.duration - span));
    $('start').value = start.toFixed(6); $('end').value = (start + span).toFixed(6);
    if (origin !== null) window.dispatchEvent(new CustomEvent('runtime-wave-zoom', {detail: {start, end: start + span}}));
    load();
  }
  function build() {
    if (origin !== null) {
      const kind = data.series.some(s => s.key === 'sent_velocity') ? 'velocity' : 'position';
      if (preset !== kind) {
        preset = kind;
        const visible = kind === 'velocity' ? ['sent_velocity', 'velocity_error'] : ['target_position', 'error'];
        groups.forEach(([, keys]) => visible.includes(keys[0]) ? hidden.delete(keys[0]) : hidden.add(keys[0]));
      }
    }
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
        return a + Math.max(0, Math.min(1, (event.clientX - rect.left - 90) / Math.max(1, rect.width - 110))) * (b - a);
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
    const w = canvas.clientWidth, h = canvas.clientHeight || 240, ratio = window.devicePixelRatio || 1;
    canvas.width = w * ratio; canvas.height = h * ratio;
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    const left = 90, right = Math.max(left + 1, w - 20), top = 20, bottom = h - 32;
    const [a, b] = bounds();
    let lo = Infinity, hi = -Infinity;
    for (const signal of signals) for (const [t, y] of signal.points) {
      if (y !== null && t >= a && t <= b) { lo = Math.min(lo, y); hi = Math.max(hi, y); }
    }
    if (!Number.isFinite(lo)) {lo = 0; hi = 1;}
    const pad = (hi - lo) * 0.08 || Math.max(Math.abs(lo) * 0.01, 0.001); lo -= pad; hi += pad;
    const x = t => left + (t - a) / (b - a) * (right - left);
    const y = v => bottom - (v - lo) / (hi - lo) * (bottom - top);
    ctx.font = '14px monospace'; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const yy = top + (bottom - top) * i / 4;
      ctx.strokeStyle = '#eaeef2'; ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(right, yy); ctx.stroke();
      const axisValue = hi - (hi - lo) * i / 4;
      const axisLabel = Math.abs(axisValue) < (hi - lo) * 1e-8 ? '0' : Number(axisValue.toPrecision(4)).toString();
      ctx.fillStyle = '#626873'; ctx.textAlign = 'right'; ctx.fillText(axisLabel, left - 7, yy + 4);
      if ((w < 380 && i !== 0 && i !== 4) || (w >= 380 && w < 600 && i % 2)) continue;
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
    if (marker !== null && marker >= a && marker <= b) {
      ctx.save(); ctx.strokeStyle = '#b93843'; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(x(marker), top); ctx.lineTo(x(marker), bottom); ctx.stroke();
      ctx.setLineDash([]); ctx.fillStyle = '#a62d39'; ctx.textAlign = marker > (a+b)/2 ? 'right' : 'left';
      ctx.fillText('选中事件', x(marker) + (marker > (a+b)/2 ? -6 : 6), top + 14); ctx.restore();
    }
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
  window.addEventListener('runtime-wave-clear', () => {
    generation++; if (controller) controller.abort(); loading = false; data = null; charts = [];
    $('charts').replaceChildren(); $('choices').replaceChildren();
    document.querySelectorAll('.wave-controls button,.wave-controls select,.wave-controls input').forEach(e => e.disabled = false);
  });
  window.addEventListener('runtime-wave-range', event => {
    const config = event.detail, previous = $('joint').value;
    endpoint = config.endpoint; origin = config.origin; marker = config.marker;
    $('joint').replaceChildren(...config.channels.map(name => {const option = document.createElement('option'); option.textContent = name; return option;}));
    if (config.channels.includes(previous)) $('joint').value = previous;
    $('start').value = config.start; $('end').value = config.end;
    load();
  });
  $('load').onclick = () => load(); $('all').onclick = () => load(true);
  $('joint').onchange = () => load();
  $('in').onclick = () => {if (data) {const [a,b] = bounds(); selectRange(a + (b-a)/4, b - (b-a)/4);}};
  $('prev').onclick = () => {if (data) {const [a,b] = bounds(); selectRange(a-(b-a), a);}};
  $('next').onclick = () => {if (data) {const [a,b] = bounds(); selectRange(b, b+(b-a));}};
  new ResizeObserver(() => charts.forEach(draw)).observe($('charts'));
  // A link from the run workbench preserves its explicit time range and origin.
  const initialQuery = new URLSearchParams(location.search);
  if (['origin', 'start', 'end'].every(key => initialQuery.has(key))) {
    const o = Number(initialQuery.get('origin')), a = Number(initialQuery.get('start')), b = Number(initialQuery.get('end'));
    if ([o, a, b].every(Number.isFinite) && o >= 0 && a >= 0 && b > a) {
      origin = o; $('start').value = a; $('end').value = b;
      const focus = initialQuery.has('focus') ? Number(initialQuery.get('focus')) : NaN;
      if (Number.isFinite(focus) && focus >= 0) marker = focus;
      const help = document.querySelector('.standalone-help p');
      if (help) help.textContent = '时间相对本次运行开始；拖拽放大，悬停读数。缺失或无效采样断线。';
    }
  }
  window.addEventListener('hashchange', () => {if (location.hash === '#waveforms' && !data) load();});
  if (location.hash === '#waveforms') load();
})();
