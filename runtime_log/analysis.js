(() => {
  'use strict';
  const $ = id => document.getElementById(id), state = JSON.parse($('analysis-data').textContent);
  const events = state.events, issues = events.map((event, index) => ({...event, index})).filter(e => e.issue);
  let selected = null, start = 0, end = state.duration, page = 0, issueLimit = 60;
  const fixed = n => Number(n).toFixed(3);
  const text = (tag, value, cls) => {const el = document.createElement(tag); el.textContent = value; if (cls) el.className = cls; return el;};
  const level = e => e.expected ? '预期切换' : e.level === 'error' || e.kind === 'failure' ? '错误' : e.level === 'warn' ? '警告' : '触发';
  function showIssues() {
    $('issue-count').textContent = issues.length;
    $('issue-list').replaceChildren();
    for (const event of issues.slice(0, issueLimit)) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'issue-button';
      button.dataset.index = event.index; button.setAttribute('aria-pressed', String(selected === event.index));
      button.append(text('span', `${fixed(event.t)} s`, 'issue-time'), text('span', level(event), `badge ${event.level === 'error' || event.kind === 'failure' ? 'danger' : event.level === 'warn' ? 'warning' : 'neutral'}`),
        text('strong', event.label), text('span', event.subject || event.source, 'k'));
      button.onclick = () => select(event.index); $('issue-list').append(button);
    }
    $('issue-more').hidden = issues.length <= issueLimit;
    if (!issues.length) $('issue-list').append(text('p', '没有异常事件；仍可按时间查看日志与归档数据。', 'empty-state'));
  }
  $('issue-more').onclick = () => {issueLimit += 60; showIssues();};
  $('duration-label').textContent = `${fixed(state.duration)} s`;
  $('time-cursor').max = state.duration;
  // Bound timeline DOM density; all events remain searchable in the paged log.
  const marks = issues.length > 160 ? issues.filter((_, i) => i % Math.ceil(issues.length / 160) === 0) : issues;
  for (const event of marks) {
    const button = document.createElement('button'); button.type = 'button'; button.className = `timeline-mark ${event.level}`;
    button.style.left = `${Math.max(0, Math.min(100, event.t / state.duration * 100))}%`;
    button.title = `${fixed(event.t)} s · ${event.label}`; button.setAttribute('aria-label', button.title);
    button.dataset.index = event.index; button.onclick = () => select(event.index); $('analysis-timeline').append(button);
  }
  function select(index) {
    selected = index;
    const event = events[index];
    $('selected-title').textContent = event.label;
    $('selected-level').textContent = level(event);
    $('selected-level').className = `badge ${event.level === 'error' || event.kind === 'failure' ? 'danger' : event.level === 'warn' ? 'warning' : 'neutral'}`;
    $('selected-time').textContent = `运行 +${fixed(event.t)} s`;
    $('selected-subject').textContent = `${event.subject || '未标注对象'} · ${event.source}`;
    $('selected-message').textContent = event.message || '此事件没有附加原文。';
    document.querySelectorAll('.issue-button,.timeline-mark').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.index) === index)));
    $('time-cursor').value = Math.max(0, event.t);
    $('evidence-select').value = '';
    setRange(Math.max(0, event.t - 10), Math.min(state.duration, Math.max(.001, event.t + 2)));
  }
  function logs() {
    const query = $('log-search').value.trim().toLowerCase();
    const rows = events.filter(e => e.t >= start && e.t <= end && (!$('log-issues').checked || e.level === 'warn' || e.level === 'error' || e.kind === 'failure') &&
      `${e.label} ${e.subject} ${e.message} ${e.source}`.toLowerCase().includes(query));
    const pages = Math.max(1, Math.ceil(rows.length / 50)); page = Math.min(page, pages - 1);
    $('log-rows').replaceChildren();
    for (const e of rows.slice(page * 50, (page + 1) * 50)) {
      const tr = document.createElement('tr'); tr.className = e.expected ? '' : e.level;
      tr.append(text('td', fixed(e.t), 'mono'), text('td', e.expected ? '预期' : e.level.toUpperCase()), text('td', e.source),
        text('td', `${e.subject ? e.subject + ' · ' : ''}${e.label}\n${e.message}`));
      $('log-rows').append(tr);
    }
    $('log-count').textContent = `${rows.length} 条`; $('log-empty').hidden = rows.length !== 0;
    $('log-page').textContent = `${page + 1} / ${pages} 页`;
    $('log-prev').disabled = page === 0; $('log-next').disabled = page >= pages - 1;
  }
  for (const [index, item] of state.evidence.entries()) {
    const option = document.createElement('option'); option.value = String(index); option.textContent = item.label; $('evidence-select').append(option);
  }
  function evidence() {
    const explicit = $('evidence-select').value;
    $('evidence-select').options[0].textContent = '自动匹配归档';
    $('evidence-select').removeAttribute('title');
    const candidates = state.evidence.filter(e => e.end >= start && e.start <= end);
    const pivot = selected === null ? (start + end) / 2 : events[selected].t;
    const ordered = [...candidates].sort((a, b) => {
      const score = e => (e.start <= pivot && e.end >= pivot ? 10 : 0) + (e.kind === 'blackbox' ? 1 : 0);
      return score(b) - score(a);
    });
    const item = explicit === '' ? ordered[0] : state.evidence[Number(explicit)];
    const available = item && item.end >= start && item.start <= end;
    $('embedded-wave').hidden = !available;
    $('evidence-empty').hidden = !available;
    $('evidence-empty').className = 'evidence-meta';
    $('evidence-detail').hidden = !item; $('evidence-csv').hidden = !item;
    if (item) {
      const query = new URLSearchParams({origin: state.t0, start, end});
      if (selected !== null) query.set('focus', events[selected].t);
      $('evidence-detail').href = item.href + '?' + query + '#waveforms';
      $('evidence-csv').href = item.csv;
    }
    if (!available) {
      $('evidence-empty').hidden = false;
      $('evidence-empty').className = 'notice';
      $('evidence-empty').textContent = item ? '归档与当前范围没有重叠。请换一份记录或调整时间。' :
        '此时段未保存波形，无法从错误日志还原采样。';
      if (!item && state.evidence.length) {
        const nearest = [...state.evidence].sort((a, b) => {
          const distance = e => Math.max(e.start - end, start - e.end, 0);
          return distance(a) - distance(b);
        })[0];
        $('evidence-empty').textContent += ` 最近归档：${fixed(nearest.start)}–${fixed(nearest.end)} s，不覆盖当前范围。`;
      }
      window.dispatchEvent(new CustomEvent('runtime-wave-clear'));
      return;
    }
    $('evidence-empty').textContent = '';
    $('evidence-empty').hidden = true;
    $('evidence-select').options[0].textContent = `自动 · ${item.label}`;
    $('evidence-select').title = `归档范围 ${fixed(item.start)}–${fixed(item.end)} s；以 MCAP 实际采样时间对齐。`;
    window.dispatchEvent(new CustomEvent('runtime-wave-range', {detail: {endpoint: item.endpoint, channels: item.channels, origin: state.t0, start, end, marker: selected === null ? null : events[selected].t}}));
  }
  function setRange(a, b, reloadWave = true) {
    if (!Number.isFinite(a) || !Number.isFinite(b) || a < 0 || b <= a || a > state.duration) {
      $('range-status').textContent = '请输入有效范围：起点不小于 0，终点大于起点，且起点在运行时长内。';
      $('range-status').className = 'notice error'; return;
    }
    start = a; end = Math.min(b, state.duration); page = 0;
    $('range-start').value = fixed(start); $('range-end').value = fixed(end);
    $('range-status').className = 'range-status';
    $('range-status').textContent = issues.length > 160 ? '时间线标记已抽稀，完整事件见左侧与日志。' : '';
    logs(); if (reloadWave) evidence();
  }
  $('range-apply').onclick = () => setRange(Number($('range-start').value), Number($('range-end').value));
  $('range-all').onclick = () => setRange(0, state.duration);
  $('range-reset').onclick = () => selected === null ? setRange(0, Math.min(12, state.duration)) : select(selected);
  $('time-cursor').onchange = () => { const t = Number($('time-cursor').value); setRange(Math.max(0, t - 10), Math.min(state.duration, t + 2)); };
  $('evidence-select').onchange = evidence;
  $('log-search').oninput = $('log-issues').onchange = () => {page = 0; logs();};
  $('log-prev').onclick = () => {page--; logs();}; $('log-next').onclick = () => {page++; logs();};
  window.addEventListener('runtime-wave-zoom', e => setRange(e.detail.start, e.detail.end, false));
  showIssues();
  if (issues.length) select((issues.find(e => e.level === 'error' || e.kind === 'failure') || issues[0]).index);
  else { $('selected-message').textContent = '此运行没有可定位的异常事件。按时间浏览日志，或选择已归档的数据记录。'; setRange(0, Math.min(12, state.duration)); }
})();
