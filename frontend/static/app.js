/* Scout app: account state, resume upload, pipeline run, results. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const splitList = (s) => s.split(',').map((v) => v.trim()).filter(Boolean);

let account = null;
let lastRun = null;
let mode = 'jobs';   // 'jobs' = openings to apply to, 'people' = contacts

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
function today() {
  return new Date().toISOString().slice(0, 10);
}

function scoreClass(score) {
  if (score == null) return 'lo';
  return score >= 75 ? 'hi' : score >= 45 ? 'mid' : 'lo';
}

function renderOpenings(data) {
  lastRun = data;
  const notes = (data.warnings || []).map((w) => `<div class="note">${esc(w)}</div>`).join('');

  if (!data.results.length) {
    $('out').innerHTML = `<div id="notes">${notes}</div>
      <div class="empty"><strong>Nothing came back</strong>
      <p>No matching openings were publicly indexed. Try a broader role, or drop the company filter.</p></div>`;
    return;
  }

  const cards = data.results.map((r, i) => {
    const cls = scoreClass(r.fit_score);
    const when = r.posted_at ? `<span class="badge">posted ${esc(r.posted_at)}</span>` : '';
    const close = r.closes_at ? `<span class="badge">closes ${esc(r.closes_at)}</span>` : '';
    // Only worth showing when the board gave no date of its own, and only once
    // it is a real prior observation — "first seen today" says nothing about
    // when the posting actually opened.
    const seen = (!r.posted_at && r.first_seen && r.first_seen.slice(0, 10) < today())
      ? `<span class="badge" title="when this app first saw the posting, not when it opened">seen since ${esc(r.first_seen.slice(0, 10))}</span>`
      : '';
    return `<article class="opening">
      <div class="opening-top">
        <div>
          <h3>${esc(r.title)}</h3>
          <div class="co">${esc(r.company || 'unknown company')}</div>
        </div>
        <div class="score ${cls}">${r.fit_score ?? '—'}<small>FIT</small></div>
      </div>
      <div class="lines">
        ${r.why ? `<div class="why">${esc(r.why)}</div>` : ''}
        ${r.gap ? `<div class="gap">${esc(r.gap)}</div>` : ''}
        ${r.error ? `<div class="rowerr">${esc(r.error)}</div>` : ''}
      </div>
      <div class="opening-foot">
        <div>
          <span class="badge">${esc(r.source)}</span>
          ${when}${close}${seen}
          ${r.priority ? `<span class="badge">${esc(r.priority)}</span>` : ''}
        </div>
        <span class="foot-actions">
          <button type="button" class="btn ghost approach" data-i="${i}">How to approach</button>
          <a class="apply" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open application →</a>
        </span>
      </div>
    </article>`;
  }).join('');

  const plan = data.plan || {};
  const chips = [...(plan.titles || []), ...(plan.companies || [])]
    .map((c) => `<span class="chip">${esc(c)}</span>`).join('');
  const queries = (data.queries_run || []).map((q) => `<div>${esc(q)}</div>`).join('');

  $('out').innerHTML = `
    <div class="results-head">
      <h2>${data.count} opening${data.count === 1 ? '' : 's'}, ranked by fit</h2>
      <button class="btn ghost" id="csv">Download CSV</button>
    </div>
    ${plan.summary ? `<p class="plan-line">${esc(plan.summary)}</p>` : ''}
    <div class="chips">${chips}</div>
    <div id="notes">${notes}</div>
    <div class="openings">${cards}</div>
    <details><summary>${(data.queries_run || []).length} searches run</summary><div>${queries}</div></details>`;
  $('csv').addEventListener('click', downloadCsv);
  bindApproach();
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

  const rows = data.results.map((r, i) => {
    const cls = scoreClass(r.fit_score);
    const prob = r.plausible_contact == null ? '—' : `${Math.round(r.plausible_contact * 100)}%`;
    const pri = r.priority ? `<span class="pri-${esc(r.priority)}">${esc(r.priority)}</span>` : '—';
    const err = r.error ? `<div class="rowerr">${esc(r.error)}</div>` : '';
    return `<div class="row">
      <div class="score ${cls}">${r.fit_score ?? '—'}<small>FIT</small></div>
      <div>
        <a class="name" href="${esc(r.linkedin_url)}" target="_blank" rel="noopener noreferrer nofollow">${esc(r.name)}</a>
        <div class="head">${esc(r.headline || r.snippet)}</div>
        ${(r.reason || r.ask) ? `<div class="rowlines">
          ${r.reason ? `<div class="reason">${esc(r.reason)}</div>` : ''}
          ${r.ask ? `<div class="ask">${esc(r.ask)}</div>` : ''}
        </div>` : ''}
        ${err}
      </div>
      <div class="meta">${esc(r.company_query)}<br>plausible ${prob} · ${pri}
        <button type="button" class="btn ghost approach" data-i="${i}">How to approach</button>
      </div>
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
  bindApproach();
}

function downloadCsv() {
  const cell = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  let header, lines, name;

  if (mode === 'jobs') {
    name = 'openings.csv';
    header = ['fit_score', 'title', 'company', 'source', 'url', 'posted_at', 'closes_at',
              'first_seen', 'matches_profile', 'priority', 'why', 'gap', 'note'];
    lines = lastRun.results.map((r) => [
      r.fit_score ?? '', r.title, r.company, r.source, r.url, r.posted_at ?? '', r.closes_at ?? '',
      r.first_seen ?? '',
      r.matches_profile == null ? '' : r.matches_profile.toFixed(3),
      r.priority ?? '', r.why ?? '', r.gap ?? '', r.error ?? '',
    ].map(cell).join(','));
  } else {
    name = 'contacts.csv';
    header = ['name', 'headline', 'company', 'linkedin_url', 'fit_score', 'plausible_contact',
              'priority', 'reason', 'ask', 'note'];
    lines = lastRun.results.map((r) => [
      r.name, r.headline, r.company_query, r.linkedin_url,
      r.fit_score ?? '', r.plausible_contact == null ? '' : r.plausible_contact.toFixed(3),
      r.priority ?? '', r.reason ?? '', r.ask ?? '', r.error ?? '',
    ].map(cell).join(','));
  }

  const blob = new Blob([[header.join(','), ...lines].join('\n')], { type: 'text/csv' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

/* ---------- how to approach ---------- */
// A ranked list says who to talk to, not what to say. This drafts it for one
// result at a time, on click -- drafting all forty up front would spend the
// model budget on cards nobody opens.

function drawer() {
  let el = $('drawer');
  if (!el) {
    el = document.createElement('div');
    el.id = 'drawer';
    el.className = 'drawer';
    el.hidden = true;
    el.addEventListener('click', (e) => { if (e.target === el) closeDrawer(); });
    document.body.appendChild(el);
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  }
  return el;
}

function closeDrawer() {
  const el = $('drawer');
  if (el) el.hidden = true;
}

function copyBlock(label, text) {
  if (!text) return '';
  return `<div class="draft-block">
    <div class="draft-label">${esc(label)}
      <button type="button" class="btn ghost copy" data-copy="${esc(text)}">Copy</button>
    </div>
    <pre>${esc(text)}</pre>
  </div>`;
}

function paintDraft(target, draft) {
  const who = target.name || target.title || 'this result';
  const points = (draft.talking_points || [])
    .map((p) => `<li>${esc(p)}</li>`).join('');
  drawer().innerHTML = `<div class="drawer-panel" role="dialog" aria-modal="true">
    <div class="drawer-head">
      <div>
        <h3>How to approach ${esc(who)}</h3>
        ${draft.headline ? `<p class="draft-angle">${esc(draft.headline)}</p>` : ''}
      </div>
      <button type="button" class="btn ghost" id="drawerClose">Close</button>
    </div>
    ${copyBlock('Connection note (fits LinkedIn’s 280 characters)', draft.connection_note)}
    ${copyBlock('Message', draft.message)}
    ${points ? `<div class="draft-block"><div class="draft-label">Talk about</div><ul>${points}</ul></div>` : ''}
    ${draft.gap ? `<div class="draft-block"><div class="draft-label">The objection to expect</div><p>${esc(draft.gap)}</p></div>` : ''}
    <p class="draft-foot">Drafted from your resume and this result only. Read it before you send it.</p>
  </div>`;
  drawer().hidden = false;
  $('drawerClose').addEventListener('click', closeDrawer);
  drawer().querySelectorAll('.copy').forEach((btn) => {
    btn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(btn.dataset.copy);
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = 'Copy'; }, 1500);
      } catch {
        btn.textContent = 'Copy failed';
      }
    });
  });
}

function bindApproach() {
  document.querySelectorAll('.approach').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const r = (lastRun && lastRun.results || [])[Number(btn.dataset.i)];
      if (!r) return;
      const original = btn.textContent;
      btn.disabled = true;
      btn.textContent = 'Drafting…';
      drawer().innerHTML = '<div class="drawer-panel"><p>Drafting…</p></div>';
      drawer().hidden = false;
      try {
        const draft = await api('/api/approach', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            kind: mode === 'jobs' ? 'opening' : 'person',
            resume: $('resume').value.trim(),
            role_target: $('role').value,
            name: r.name || '',
            headline: r.headline || '',
            title: r.title || '',
            company: r.company || r.company_query || '',
            snippet: r.snippet || '',
            posted_at: r.posted_at || '',
            url: r.url || r.linkedin_url || '',
          }),
        });
        paintDraft(r, draft);
      } catch (err) {
        drawer().innerHTML = `<div class="drawer-panel">
          <div class="drawer-head"><h3>Could not draft that</h3>
          <button type="button" class="btn ghost" id="drawerClose">Close</button></div>
          <div class="note bad">${esc(err.message)}</div></div>`;
        $('drawerClose').addEventListener('click', closeDrawer);
      } finally {
        btn.disabled = false;
        btn.textContent = original;
      }
    });
  });
}

/* ---------- mode ---------- */
// Both of these were referenced (paintMode on load, MODE_COPY after every run)
// but never defined, so the toggle did nothing, the ReferenceError on load also
// killed loadAccount(), and the run button stayed stuck on "Running…".
const MODE_COPY = {
  jobs: {
    go: 'Find openings',
    note: 'Finds live postings on company job boards — each one opens straight onto an application form.',
    titles: 'Roles',
    titlesHint: 'software engineer intern',
  },
  people: {
    go: 'Find people',
    note: 'Finds people who can refer or screen you — recruiters, hiring managers, engineers on the team.',
    titles: 'Titles to contact',
    titlesHint: 'university recruiter',
  },
};

function paintMode() {
  const copy = MODE_COPY[mode];
  document.querySelectorAll('.mode').forEach((btn) => {
    const on = btn.dataset.mode === mode;
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('modeNote').textContent = copy.note;
  $('go').textContent = copy.go;
  $('titlesLabel').innerHTML = `${esc(copy.titles)} <span class="opt">optional</span>`;
  $('titles').placeholder = copy.titlesHint;
  // Results from the other mode would be rendered by the wrong renderer.
  $('out').innerHTML = '';
  lastRun = null;
}

document.querySelectorAll('.mode').forEach((btn) => {
  btn.addEventListener('click', () => {
    if (btn.dataset.mode === mode) return;
    mode = btn.dataset.mode;
    paintMode();
  });
});

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
  if (!useAgent && !companies.length && mode === 'people') {
    banner('With the agent off, you need to name at least one company yourself.');
    return;
  }
  if (!useAgent && !companies.length && !splitList($('titles').value).length) {
    banner('With the agent off, name at least one role or company to search for.');
    return;
  }

  $('go').disabled = true;
  $('go').textContent = 'Running…';
  startProgress(useAgent);

  try {
    const data = await api(mode === 'jobs' ? '/api/openings' : '/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        mode,
        resume,
        role_target: $('role').value,
        companies,
        titles: splitList($('titles').value),
        per_query_results: Number($('perQuery').value),
        max_candidates: Number($('maxCandidates').value),
        use_agent: useAgent,
        fresh_only: $('freshOnly').checked,
      }),
    });
    account.runs_used = data.runs_used;
    account.runs_allowed = data.runs_allowed;
    paintAccount();
    (mode === 'jobs' ? renderOpenings : render)(data);
  } catch (err) {
    $('out').innerHTML = `<div class="note bad">${esc(err.message)}</div>`;
    if (/searches this month/i.test(err.message) && !$('upgrade').hidden) {
      banner('Out of runs for this month — upgrade for 250.', 'bad');
    }
  } finally {
    stopProgress();
    $('go').disabled = false;
    $('go').textContent = MODE_COPY[mode].go;
  }
});

paintMode();
loadAccount().catch((err) => banner(err.message));
