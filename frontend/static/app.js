/* Scout app: account state, resume upload, pipeline run, results. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const splitList = (s) => s.split(',').map((v) => v.trim()).filter(Boolean);

let account = null;
let lastRun = null;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401) { location.href = '/login'; throw new Error('Signed out'); }
  if (!res.ok) throw new Error((data && data.detail) || `HTTP ${res.status}`);
  return data;
}

/* ---------- account ---------- */
function paintAccount() {
  if (!account) return;
  $('email').textContent = account.email;
  $('plan').textContent = account.plan;
  $('plan').classList.toggle('pro', account.plan === 'pro');
  $('quota').textContent = `${account.runs_used} / ${account.runs_allowed} runs this month`;
  $('priceLabel').textContent = account.price_label;
  $('upgrade').hidden = account.plan === 'pro' || !account.billing_enabled;
  $('manage').hidden = account.plan !== 'pro';
  $('maxCandidates').max = account.plan === 'pro' ? 200 : 60;
}

function banner(text, kind = 'bad') {
  $('banner').innerHTML = text ? `<div class="note ${kind}" style="margin-bottom:16px">${esc(text)}</div>` : '';
}

async function loadAccount() {
  account = await api('/api/account');
  paintAccount();

  const checkout = new URLSearchParams(location.search).get('checkout');
  if (checkout === 'success' && account.plan !== 'pro') {
    banner("Payment received — Stripe is confirming it. Your plan flips to Pro within a few seconds; refresh if it doesn't.", 'good');
  } else if (checkout === 'success') {
    banner('You’re on Pro. 250 runs a month.', 'good');
  } else if (checkout === 'cancelled') {
    banner('Checkout cancelled — nothing was charged.');
  }
  if (checkout) history.replaceState({}, '', '/app');
}

$('logout').addEventListener('click', async () => {
  await api('/api/auth/logout', { method: 'POST' });
  location.href = '/';
});

$('upgrade').addEventListener('click', async () => {
  $('upgrade').disabled = true;
  try {
    const { url } = await api('/api/billing/checkout', { method: 'POST' });
    location.href = url;
  } catch (err) {
    banner(err.message);
    $('upgrade').disabled = false;
  }
});

$('manage').addEventListener('click', async () => {
  try {
    const { url } = await api('/api/billing/portal', { method: 'POST' });
    location.href = url;
  } catch (err) { banner(err.message); }
});

/* ---------- resume upload ---------- */
const drop = $('drop');

async function handleFile(file) {
  if (!file) return;
  drop.classList.remove('done');
  $('dropTitle').textContent = `Reading ${file.name}…`;
  $('dropSub').textContent = 'This can take a few seconds for images.';

  const body = new FormData();
  body.append('file', file);
  try {
    const data = await api('/api/resume', { method: 'POST', body });
    $('resume').value = data.resume;
    drop.classList.add('done');
    $('dropTitle').textContent = `${file.name} — ${data.chars.toLocaleString()} characters`;
    $('dropSub').textContent =
      data.source === 'vision-ocr'
        ? 'Read by the vision model — check the text below before running.'
        : 'Read from the PDF text layer — check the text below before running.';
    $('resume').scrollIntoView({ behavior: 'smooth', block: 'center' });
  } catch (err) {
    $('dropTitle').textContent = 'Drop a PDF or photo of your resume';
    $('dropSub').textContent = 'PDF text layer is read locally · images go to a vision model';
    banner(err.message);
  }
}

drop.addEventListener('click', () => $('file').click());
drop.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('file').click(); } });
$('file').addEventListener('change', (e) => handleFile(e.target.files[0]));
['dragenter', 'dragover'].forEach((t) =>
  drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach((t) =>
  drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', (e) => handleFile(e.dataTransfer.files[0]));

/* ---------- progress ---------- */
let stageTimers = [];

function setStage(name) {
  const order = ['plan', 'source', 'score'];
  const idx = order.indexOf(name);
  document.querySelectorAll('.stage').forEach((el) => {
    const i = order.indexOf(el.dataset.stage);
    el.classList.toggle('active', i === idx);
    el.classList.toggle('done', i < idx);
  });
}

function startProgress(useAgent) {
  $('progress').hidden = false;
  $('out').innerHTML = '';
  setStage(useAgent ? 'plan' : 'source');
  // The API is a single call, so stages are advanced on their typical timing.
  stageTimers = [
    setTimeout(() => setStage('source'), useAgent ? 3500 : 0),
    setTimeout(() => setStage('score'), useAgent ? 9000 : 5500),
  ];
}

function stopProgress() {
  stageTimers.forEach(clearTimeout);
  stageTimers = [];
  $('progress').hidden = true;
}

/* ---------- results ---------- */
function scoreClass(score) {
  if (score == null) return 'lo';
  return score >= 75 ? 'hi' : score >= 45 ? 'mid' : 'lo';
}

function render(data) {
  lastRun = data;
  const notes = (data.warnings || []).map((w) => `<div class="note">${esc(w)}</div>`).join('');

  if (!data.results.length) {
    $('out').innerHTML = `<div id="notes">${notes}</div>
      <div class="empty"><strong>Nothing came back</strong>
      <p>No public profiles matched. Try broader titles, or add companies by name.</p></div>`;
    return;
  }

  const rows = data.results.map((r) => {
    const cls = scoreClass(r.fit_score);
    const prob = r.plausible_contact == null ? '—' : `${Math.round(r.plausible_contact * 100)}%`;
    const pri = r.priority ? `<span class="pri-${esc(r.priority)}">${esc(r.priority)}</span>` : '—';
    const err = r.error ? `<div class="rowerr">${esc(r.error)}</div>` : '';
    return `<div class="row">
      <div class="score ${cls}">${r.fit_score ?? '—'}<small>FIT</small></div>
      <div>
        <a class="name" href="${esc(r.linkedin_url)}" target="_blank" rel="noopener noreferrer nofollow">${esc(r.name)}</a>
        <div class="head">${esc(r.headline || r.snippet)}</div>
        ${err}
      </div>
      <div class="meta">${esc(r.company_query)}<br>plausible ${prob} · ${pri}</div>
    </div>`;
  }).join('');

  const plan = data.plan || {};
  const chips = [...(plan.companies || []), ...(plan.titles || [])]
    .map((c) => `<span class="chip">${esc(c)}</span>`).join('');
  const queries = (data.queries_run || []).map((q) => `<div>${esc(q)}</div>`).join('');

  $('out').innerHTML = `
    <div class="results-head">
      <h2>${data.count} contact${data.count === 1 ? '' : 's'}, ranked by fit</h2>
      <button class="btn ghost" id="csv">Download CSV</button>
    </div>
    ${plan.summary ? `<p class="plan-line">${esc(plan.summary)}${plan.agent_used ? '' : ' (agent not used)'}</p>` : ''}
    <div class="chips">${chips}</div>
    <div id="notes">${notes}</div>
    <div class="rows">${rows}</div>
    <details><summary>${(data.queries_run || []).length} search queries run</summary><div>${queries}</div></details>`;

  $('csv').addEventListener('click', downloadCsv);
}

function downloadCsv() {
  const cell = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  const header = ['name', 'headline', 'company', 'linkedin_url', 'fit_score', 'plausible_contact', 'priority', 'note'];
  const lines = lastRun.results.map((r) => [
    r.name, r.headline, r.company_query, r.linkedin_url,
    r.fit_score ?? '', r.plausible_contact == null ? '' : r.plausible_contact.toFixed(3),
    r.priority ?? '', r.error ?? '',
  ].map(cell).join(','));
  const blob = new Blob([[header.join(','), ...lines].join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'contacts.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------- run ---------- */
$('form').addEventListener('submit', async (e) => {
  e.preventDefault();
  banner('');

  const resume = $('resume').value.trim();
  if (resume.length < 20) {
    banner('Add your resume first — upload a PDF or image, or paste the text.');
    return;
  }

  const useAgent = $('useAgent').checked;
  const companies = splitList($('companies').value);
  if (!useAgent && !companies.length) {
    banner('With the agent off, you need to name at least one company yourself.');
    return;
  }

  $('go').disabled = true;
  $('go').textContent = 'Running…';
  startProgress(useAgent);

  try {
    const data = await api('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        resume,
        role_target: $('role').value,
        companies,
        titles: splitList($('titles').value),
        per_query_results: Number($('perQuery').value),
        max_candidates: Number($('maxCandidates').value),
        use_agent: useAgent,
      }),
    });
    account.runs_used = data.runs_used;
    account.runs_allowed = data.runs_allowed;
    paintAccount();
    render(data);
  } catch (err) {
    $('out').innerHTML = `<div class="note bad">${esc(err.message)}</div>`;
    if (/searches this month/i.test(err.message) && !$('upgrade').hidden) {
      banner('Out of runs for this month — upgrade for 250.', 'bad');
    }
  } finally {
    stopProgress();
    $('go').disabled = false;
    $('go').textContent = 'Run the pipeline';
  }
});

loadAccount().catch((err) => banner(err.message));
