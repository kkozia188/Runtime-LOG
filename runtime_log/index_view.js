(() => {
  'use strict';
  const $ = id => document.getElementById(id), picked = [];
  const rows = [...document.querySelectorAll('#runs tbody tr')];
  function filter() {
    const text = $('fText').value.trim().toLowerCase();
    let shown = 0;
    for (const row of rows) {
      row.hidden = !!(($('fResult').value && row.dataset.result !== $('fResult').value) ||
        ($('fHw').value && row.dataset.hw !== $('fHw').value) ||
        ($('fAnomaly').checked && row.dataset.issues !== '1') || !row.textContent.toLowerCase().includes(text));
      if (!row.hidden) shown++;
    }
    $('visible-count').textContent = `${shown} / ${rows.length}`;
    $('filter-empty').hidden = shown !== 0;
  }
  ['fResult', 'fHw', 'fAnomaly', 'fText'].forEach(id => $(id).addEventListener('input', filter));
  $('toggle-columns').onclick = () => {
    const expanded = $('runs').classList.toggle('show-details');
    $('toggle-columns').setAttribute('aria-pressed', String(expanded));
    $('toggle-columns').textContent = expanded ? '收起详细字段' : '详细字段';
  };
  let historyStale = false;
  function navigation() {
    const active = location.hash === '#history' ? 'runs' : 'issues';
    $('current-run').hidden = active === 'runs';
    $('history').hidden = active !== 'runs';
    $('home-title').textContent = active === 'runs' ? '运行历史' : '本次运行';
    $('index-update').hidden = !historyStale || active !== 'runs';
    document.querySelectorAll('[data-nav]').forEach(link => {
      if (link.dataset.nav === active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
  }
  window.addEventListener('hashchange', navigation); navigation();
  document.querySelectorAll('.pick').forEach(box => box.onchange = () => {
    const old = picked.indexOf(box.value); if (old >= 0) picked.splice(old, 1);
    if (box.checked) picked.push(box.value);
    if (picked.length > 2) picked.shift();
    document.querySelectorAll('.pick').forEach(item => item.checked = picked.includes(item.value));
    $('compareBtn').disabled = picked.length !== 2;
    $('picked-note').textContent = picked.length ? `已选 ${picked.length} 次 · 先选为 A` : '';
  });
  $('compareBtn').onclick = () => { location.href = '/compare?' + new URLSearchParams({a: picked[0], b: picked[1]}); };
  $('refresh-index').onclick = () => location.reload();
  let generated = JSON.parse($('index-state').textContent).generated;
  async function poll() {
    try {
      if (!document.hidden) {
        const response = await fetch('/index.json', {cache: 'no-store'});
        if (response.ok) {
          const latest = await response.json();
          if (latest.generated !== generated) {
            // Refresh only the current-run summary. Historical filters and selections stay intact.
            const html = await fetch('/', {cache: 'no-store'});
            if (!html.ok) throw new Error('无法刷新本次运行');
            const next = new DOMParser().parseFromString(await html.text(), 'text/html');
            const current = next.getElementById('current-run');
            if (!current) throw new Error('缺少本次运行');
            $('current-run').replaceChildren(...current.childNodes);
            const stamp = next.querySelector('.workspace-state');
            if (stamp) document.querySelector('.workspace-state').textContent = stamp.textContent;
            const footer = next.querySelector('.footer');
            if (footer) document.querySelector('.footer').replaceChildren(...footer.childNodes);
            generated = JSON.parse(next.getElementById('index-state').textContent).generated;
            historyStale = true;
            navigation();
          }
        }
      }
    } catch (_) { /* Keep existing archive and filter state usable while offline. */ }
    setTimeout(poll, 15000);
  }
  if (location.protocol !== 'file:') setTimeout(poll, 15000);
})();
