let token = '', runs = [], selected = null, filter = 'all', timer = null;
const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const money = cents => cents == null ? '-' : (cents / 100).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const amount = i => `${i.currency || ''} ${money(i.total_cents)}`.trim();
const label = s => s.replaceAll('_', ' ');
const badge = status => `<span class="badge ${esc(status)}">${esc(label(status))}</span>`;
const BLOCKED = ['rejected', 'duplicate', 'error'];
const say = (text, error = false) => { $('message').textContent = text; $('message').className = error ? 'error' : ''; };
const AGENTS = {ingestion: 'Ingestion', validation: 'Validation', approval: 'Approval', policy: 'Policy gate',
                payment: 'Payment', review: 'Review queue', human: 'Human', orchestrator: 'Orchestrator', operator: 'Operator'};

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw Error(data.error || 'Request failed');
  return data;
}

function headline(r) {
  if (r.status === 'processing') return 'Agents working…';
  if (r.error) return r.error;
  const codes = (r.findings || []).filter(f => f.severity !== 'info').map(f => f.code);
  return codes.length ? codes.join(' · ') : (r.payment ? `Paid · ${r.payment.reference}` : '');
}

async function refresh() {
  try {
    const data = await request('/api/runs'); token = data.token; runs = data.runs;
    $('mode').textContent = data.mode === 'gemini' ? `gemini agents · ${data.model}` : 'offline · rules stand-ins';
    const done = runs.filter(r => r.status !== 'processing');
    const paid = runs.filter(r => r.status === 'paid' && !r.review).length;
    const times = done.filter(r => r.elapsed_ms).map(r => r.elapsed_ms);
    $('count').textContent = runs.length;
    $('speed').textContent = times.length ? `avg ${(times.reduce((a, b) => a + b, 0) / times.length / 1000).toFixed(1)}s each` : '';
    $('paid').textContent = paid;
    $('stp').textContent = done.length ? `${Math.round(100 * paid / done.length)}% straight-through` : '';
    $('pending').textContent = runs.filter(r => r.status === 'pending_review').length;
    const blocked = runs.filter(r => BLOCKED.includes(r.status));
    $('blocked').textContent = blocked.length;
    const stopped = blocked.filter(r => r.invoice?.total_cents > 0).reduce((s, r) => s + r.invoice.total_cents, 0);
    $('saved').textContent = stopped ? `${money(stopped)} kept from payment` : '';
    const shown = runs.filter(r => filter === 'all' || r.status === filter || (filter === 'blocked' && BLOCKED.includes(r.status)));
    $('queue').innerHTML = shown.length ? shown.map(r => `<button class="row" data-id="${esc(r.id)}" aria-pressed="${r.id === selected}">
      <span class="row-top"><strong>${esc(r.invoice?.invoice_number || r.filename)}</strong>${badge(r.status)}</span>
      <small>${esc(r.invoice?.vendor || r.filename)}${r.invoice ? ' · ' + esc(amount(r.invoice)) : ''}</small>
      <small class="why">${esc(headline(r))}</small></button>`).join('') : '<p class="empty">Nothing here.</p>';
    document.querySelectorAll('[data-id]').forEach(b => b.onclick = () => { selected = b.dataset.id; render(); refresh(); });
    render();
    clearTimeout(timer);
    if (runs.some(r => r.status === 'processing')) timer = setTimeout(refresh, 1200);
  } catch (e) { say(e.message, true); }
}

function findingsHTML(findings) {
  if (!findings.length) return '';
  const order = {block: 0, hold: 1, info: 2};
  return `<ul class="findings">${[...findings].sort((a, b) => order[a.severity] - order[b.severity]).map(f =>
    `<li class="sev-${esc(f.severity)}"><span class="code">${esc(f.code)}</span>${esc(f.message)}</li>`).join('')}</ul>`;
}

function timelineHTML(r) {
  return `<ol class="timeline">${r.events.map(e => `<li class="ag-${esc(e.agent)}">
    <div class="step"><span class="who">${esc(AGENTS[e.agent] || e.agent)}</span><span class="what">${esc(e.summary || label(e.action))}</span>
    ${e.ms != null ? `<span class="ms">${(e.ms / 1000).toFixed(1)}s</span>` : ''}</div>
    ${e.detail ? `<details><summary>${esc(label(e.action))} details</summary><pre>${esc(JSON.stringify(e.detail, null, 2))}</pre></details>` : ''}
  </li>`).join('')}${r.status === 'processing' ? '<li class="working"><div class="step"><span class="who">…</span><span class="what">Working</span></div></li>' : ''}</ol>`;
}

function render() {
  const r = runs.find(r => r.id === selected); if (!r) return;
  const i = r.invoice, d = r.decision;
  const holds = (r.findings || []).filter(f => f.severity === 'hold');
  $('detail').innerHTML = `<p class="eyebrow">DECISION RECORD · ${esc(r.filename)}</p>
    <div class="title-row"><h2>${esc(i?.invoice_number || r.filename)}</h2>${badge(r.status)}</div>
    ${d ? `<div class="verdict out-${esc(d.outcome)}"><strong>${esc({approve: 'Approved', review: 'Needs a person', reject: 'Rejected'}[d.outcome] || d.outcome)}</strong>
      <span>${esc(d.reason)}</span><small>Decided by ${esc(d.by)}</small></div>` : ''}
    ${r.error ? `<div class="verdict out-reject"><strong>Not processed</strong><span>${esc(r.error)}</span>
      <small><button class="secondary" id="recover">Release and allow resubmission</button></small></div>` : ''}
    ${findingsHTML(r.findings || [])}
    ${i ? `<dl class="facts"><div><dt>Vendor</dt><dd>${esc(i.vendor || 'Missing')}</dd></div>
      <div><dt>Amount</dt><dd>${esc(amount(i))}${i.currency_assumed ? ' <small>(currency assumed)</small>' : ''}</dd></div>
      <div><dt>Due date</dt><dd>${esc(i.due_date || 'Missing or invalid')}</dd></div>
      <div><dt>Payment</dt><dd>${esc(r.payment ? `${r.payment.reference}` : 'None')}</dd></div></dl>
      <table><thead><tr><th>Item</th><th>Qty</th><th>Unit price</th><th>Line total</th></tr></thead><tbody>
      ${i.items.map(l => `<tr><td>${esc(l.item)}${l.description ? `<small> · ${esc(l.description)}</small>` : ''}</td><td>${esc(l.quantity)}</td>
        <td>${money(l.unit_price_cents)}</td><td>${l.quantity != null && l.unit_price_cents != null ? money(l.quantity * l.unit_price_cents) : '-'}</td></tr>`).join('')}
      </tbody></table>` : ''}
    ${r.status === 'pending_review' ? `<form id="review" class="review"><h2>Your decision</h2>
      <p>Resolve ${holds.length === 1 ? 'this item' : `these ${holds.length} items`} before approving: ${holds.map(f => esc(f.code)).join(', ')}.</p>
      <label for="reviewer">Reviewer name</label><input id="reviewer" required maxlength="100" autocomplete="name">
      <label for="reason">Rationale</label><textarea id="reason" required maxlength="2000" placeholder="e.g. Confirmed rush-order pricing with purchasing"></textarea>
      <div><button name="decision" value="approve">Approve and pay</button> <button class="secondary" name="decision" value="reject">Reject</button></div></form>` : ''}
    ${r.review ? `<p class="reviewed">Reviewed by ${esc(r.review.reviewer)}: ${esc(r.review.reason)}</p>` : ''}
    <h2 class="trail">Agent trail${r.usage ? ` <small>${r.usage.api_calls} model calls · ${r.usage.prompt_tokens + r.usage.output_tokens} tokens</small>` : ''}${r.elapsed_ms ? ` <small>${(r.elapsed_ms / 1000).toFixed(1)}s</small>` : ''}</h2>
    ${timelineHTML(r)}
    <details><summary>Full structured record (JSON)</summary><pre>${esc(JSON.stringify(r, null, 2))}</pre></details>`;
  if ($('review')) $('review').onsubmit = async e => {
    e.preventDefault();
    try {
      await request('/api/review', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
        body: JSON.stringify({id: r.id, decision: e.submitter.value, reviewer: $('reviewer').value, reason: $('reason').value})});
      say('Decision recorded.'); await refresh();
    } catch (err) { say(err.message, true); }
  };
  if ($('recover')) $('recover').onclick = async () => {
    try {
      await request('/api/recover', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': token},
        body: JSON.stringify({id: r.id})});
      say('Released. Upload the file again to retry.'); await refresh();
    } catch (err) { say(err.message, true); }
  };
}

$('upload').onsubmit = async e => {
  e.preventDefault();
  const files = [...$('file').files]; if (!files.length) return;
  $('process').disabled = true; say('');
  try {
    for (const file of files) {
      if (file.size > 5 * 1024 * 1024) { say(`${file.name} exceeds 5 MB.`, true); continue; }
      const r = await request('/api/process', {method: 'POST', body: file,
        headers: {'X-CSRF-Token': token, 'X-Filename': encodeURIComponent(file.name)}});
      selected = r.id;
      if (r.replayed) say(`${file.name} was already processed; showing the original result.`);
    }
    $('upload').reset(); await refresh();
  } catch (err) { say(err.message, true); }
  finally { $('process').disabled = false; }
};
document.querySelectorAll('[data-filter]').forEach(b => b.onclick = () => {
  filter = b.dataset.filter;
  document.querySelectorAll('[data-filter]').forEach(x => x.setAttribute('aria-pressed', x === b));
  refresh();
});
refresh();
