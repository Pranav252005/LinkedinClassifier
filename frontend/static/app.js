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

// Runs outlast the proxy's timeout, so the server starts one and the page
// polls for it instead of holding a single request open.
async function runInBackground(options) {
  const { id } = await api('/api/runs', options);
  for (;;) {
    await new Promise((r) => setTimeout(r, 2000));
    const job = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (job.status === 'done') return job.result;
  }
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

/* ---------- profile ---------- */
// The resume, read once into fields the hard filters use. Shown as editable
// chips so a wrong level or a missing city is fixed before it costs a run.
let profile = null;
const LISTS = ['target_roles', 'skills', 'locations'];
const chipbox = (name) => document.querySelector(`.chipbox[data-list="${name}"]`);

function renderChips(name) {
  const box = chipbox(name);
  const items = (profile && profile[name]) || [];
  box.innerHTML = items.map((v, i) =>
    `<span class="tag">${esc(v)}<button type="button" data-i="${i}" aria-label="Remove ${esc(v)}">×</button></span>`
  ).join('') + '<input type="text" placeholder="add…">';
  box.querySelectorAll('.tag button').forEach((b) => b.addEventListener('click', () => {
    profile[name].splice(Number(b.dataset.i), 1);
    renderChips(name);
  }));
  const input = box.querySelector('input');
  const add = () => {
    splitList(input.value).forEach((v) => {
      if (!profile[name].some((x) => x.toLowerCase() === v.toLowerCase())) profile[name].push(v);
    });
    input.value = '';
    renderChips(name);
    chipbox(name).querySelector('input').focus();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); if (input.value.trim()) add(); }
    if (e.key === 'Backspace' && !input.value && profile[name].length) {
      profile[name].pop();
      renderChips(name);
      chipbox(name).querySelector('input').focus();
    }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) add(); });
}

function paintProfile() {
  if (!profile) return;
  LISTS.forEach((n) => { profile[n] = profile[n] || []; renderChips(n); });
  $('pSeniority').value = profile.seniority || 'new_grad';
  $('pYears').value = profile.years_experience ?? 0;
  $('pIntern').checked = (profile.job_types || []).includes('internship');
  $('pFull').checked = (profile.job_types || []).includes('full_time');
  $('pRemote').checked = profile.remote_ok !== false;
  $('pSponsor').checked = !!profile.needs_sponsorship;
  $('pPaid').checked = !!profile.paid_only;
  $('pMode').value = profile.work_mode || 'any';
  $('pCollege').value = profile.college || '';
  $('pGrad').value = profile.grad_year || '';
  $('profileSummary').textContent = profile.summary
    ? `${profile.summary} — edit anything that's wrong.`
    : 'Edit anything that’s wrong — openings are filtered against these fields.';
  $('profileFields').hidden = false;
  $('readProfile').textContent = 'Re-read from resume';
}

function readProfileForm() {
  if (!profile) return null;
  const types = [];
  if ($('pIntern').checked) types.push('internship');
  if ($('pFull').checked) types.push('full_time');
  profile.seniority = $('pSeniority').value;
  profile.years_experience = Math.max(0, Number($('pYears').value) || 0);
  profile.job_types = types.length ? types : ['full_time'];
  profile.remote_ok = $('pRemote').checked;
  profile.needs_sponsorship = $('pSponsor').checked;
  profile.paid_only = $('pPaid').checked;
  profile.work_mode = $('pMode').value;
  profile.college = $('pCollege').value.trim();
  const grad = Number($('pGrad').value);
  profile.grad_year = grad >= 1970 && grad <= 2100 ? grad : null;
  return profile;
}

$('readProfile').addEventListener('click', async () => {
  const resume = $('resume').value.trim();
  if (resume.length < 20) { banner('Add your resume first, then read it into a profile.'); return; }
  const btn = $('readProfile');
  btn.disabled = true;
  btn.textContent = 'Reading…';
  try {
    profile = await api('/api/profile', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ resume }),
    });
    paintProfile();
  } catch (err) {
    banner(err.message);
    btn.textContent = profile ? 'Re-read from resume' : 'Read from resume';
  } finally {
    btn.disabled = false;
  }
});

async function loadProfile() {
  const data = await api('/api/profile');
  if (data && data.profile) { profile = data.profile; paintProfile(); }
}

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

/* ---------- feedback ---------- */
// Every result can be marked. The labels are what the ranking is measured and,
// eventually, re-weighted against — see backend/evaluation.py and weights.py.
const FB_OPENING = [['rating', 1, '👍', 'Good match'], ['rating', -1, '👎', 'Not for me'],
                    ['applied', true, 'Applied', 'I applied'], ['replied', true, 'Heard back', 'Got a reply']];
const FB_PERSON = [['rating', 1, '👍', 'Good contact'], ['rating', -1, '👎', 'Wrong person'],
                   ['messaged', true, 'Messaged', 'I messaged them'], ['replied', true, 'Replied', 'They replied']];

function fbButtons(kind, i, state) {
  const opts = kind === 'opening' ? FB_OPENING : FB_PERSON;
  const st = state || {};
  return `<span class="fb" data-kind="${kind}" data-i="${i}">${opts.map(([field, value, label, title]) => {
    const on = field === 'rating' ? st.rating === value : !!st[field];
    return `<button type="button" class="${on ? 'on' : ''}" data-field="${field}" data-value="${value}"
      title="${esc(title)}" aria-pressed="${on}">${label}</button>`;
  }).join('')}</span>`;
}

function bindFeedback(results, keyOf, urlOf, titleOf) {
  document.querySelectorAll('.fb').forEach((group) => {
    group.querySelectorAll('button').forEach((btn) => btn.addEventListener('click', async () => {
      const r = results[Number(group.dataset.i)];
      if (!r) return;
      const st = r.feedback || {};
      const field = btn.dataset.field;
      let value;
      if (field === 'rating') {
        const v = Number(btn.dataset.value);
        value = st.rating === v ? 0 : v;           // click again to clear
      } else {
        value = !st[field];
      }
      btn.disabled = true;
      try {
        r.feedback = await api('/api/feedback', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: group.dataset.kind, item_key: keyOf(r), run_id: lastRun && lastRun.run_id,
                                 url: urlOf(r), title: titleOf(r), [field]: value }),
        });
        group.outerHTML = fbButtons(group.dataset.kind, group.dataset.i, r.feedback);
        bindFeedback(results, keyOf, urlOf, titleOf);
      } catch (err) {
        banner(err.message);
        btn.disabled = false;
      }
    }));
  });
}

const LEVEL_WORD = { fit: 'fits', under: 'a stretch', over: 'below your level' };

function reasonsLine(r) {
  const parts = [];
  if (r.skills_matched && r.skills_matched.length) {
    parts.push(`<span><span class="lbl">Matches:</span> <span class="ok">${esc(r.skills_matched.join(', '))}</span></span>`);
  }
  if (r.skills_missing && r.skills_missing.length) {
    parts.push(`<span><span class="lbl">Missing:</span> <span class="miss">${esc(r.skills_missing.join(', '))}</span></span>`);
  }
  if (r.level_fit) parts.push(`<span><span class="lbl">Level:</span> ${esc(LEVEL_WORD[r.level_fit] || r.level_fit)}</span>`);
  if (r.location) parts.push(`<span><span class="lbl">Where:</span> ${esc(r.location)}</span>`);
  return parts.length ? `<div class="reasons">${parts.join('<span class="lbl">·</span>')}</div>` : '';
}

function renderOpenings(data) {
  lastRun = data;
  const notes = (data.warnings || []).map((w) => `<div class="note">${esc(w)}</div>`).join('');

  if (!data.results.length) {
    $('out').innerHTML = `<div id="notes">${notes}</div>
      <div class="empty"><strong>Nothing came back</strong>
      <p>Nothing survived for this profile. Check the notes above: they say what was hidden and why.</p></div>`;
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
    const src = r.verified
      ? `<span class="badge live" title="listed on the company's own job board during this run">live · ${esc(r.source)}</span>`
      : `<span class="badge web" title="found by web search; the company's board could not be read">web result</span>`;
    const pay = r.pay === 'paid'
      ? '<span class="badge live" title="the posting mentions pay, a salary or a stipend">paid</span>'
      : r.pay === 'unpaid'
        ? '<span class="badge web" title="the posting says it is unpaid">unpaid</span>'
        : '';
    const mode = r.work_mode
      ? `<span class="badge" title="${r.checked ? 'read from the posting page' : 'read from the listing'}">${esc(r.work_mode)}${r.location ? ` · ${esc(r.location)}` : ''}</span>`
      : '';
    const checked = r.checked
      ? `<span class="badge live" title="${esc(r.check_note || 'the full posting was read to confirm mode, location and pay')}">checked</span>`
      : '<span class="badge quick" title="the posting page was not read; mode, location and pay are unconfirmed">unchecked</span>';
    const quick = r.reviewed ? '' :
      '<span class="badge quick" title="ranked by similarity only; the full description was not reviewed">quick match</span>';
    return `<article class="opening">
      <div class="opening-top">
        <div>
          <h3>${esc(r.title)}</h3>
          <div class="co">${esc(r.company || 'unknown company')}${r.department ? ` · ${esc(r.department)}` : ''}</div>
        </div>
        <div class="score ${cls}">${r.fit_score ?? '—'}<small>FIT</small></div>
      </div>
      <div class="lines">
        ${reasonsLine(r)}
        ${r.why ? `<div class="why">${esc(r.why)}</div>` : ''}
        ${r.gap ? `<div class="gap">${esc(r.gap)}</div>` : ''}
        ${r.timing_note ? `<div class="timing">${esc(r.timing_note)}</div>` : ''}
        ${r.error ? `<div class="rowerr">${esc(r.error)}</div>` : ''}
      </div>
      <div class="opening-foot">
        <div>${src}${checked}${mode}${pay}${quick}${when}${close}${seen}</div>
        <span class="foot-actions">
          ${fbButtons('opening', i, r.feedback)}
          <button type="button" class="btn ghost approach people-btn" data-i="${i}"
            title="The recruiter, manager and team members for this role">People for this role</button>
          <button type="button" class="btn ghost approach" data-i="${i}">How to approach</button>
          <a class="apply" href="${esc(r.apply_url || r.url)}" target="_blank" rel="noopener noreferrer">Open application →</a>
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
      <span class="foot-actions">
        <button class="btn ghost" id="saveSearch" title="Check these boards daily and alert you to new matches">Alert me to new matches</button>
        <button class="btn ghost" id="csv">Download CSV</button>
      </span>
    </div>
    ${plan.summary ? `<p class="plan-line">${esc(plan.summary)}</p>` : ''}
    <div class="chips">${chips}</div>
    <div id="notes">${notes}</div>
    <div class="openings">${cards}</div>
    <details><summary>${(data.boards_read || []).length} job boards read${(data.queries_run || []).length ? `, ${data.queries_run.length} web searches` : ''}</summary>
      <div>${(data.boards_read || []).map((b) => `<div>${esc(b)}</div>`).join('')}${queries}</div></details>`;
  $('csv').addEventListener('click', downloadCsv);
  $('saveSearch').addEventListener('click', saveSearch);
  bindApproach();
  bindPeople();
  bindFeedback(data.results, (r) => r.key, (r) => r.url, (r) => `${r.title} — ${r.company}`);
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
        ${r.alumni ? '<span class="badge alum" title="the public snippet mentions your college">alumni</span>' : ''}
        ${r.location_match ? '<span class="badge">nearby</span>' : ''}
        <div class="head">${esc(r.headline || r.snippet)}</div>
        ${(r.reason || r.ask) ? `<div class="rowlines">
          ${r.reason ? `<div class="reason">${esc(r.reason)}</div>` : ''}
          ${r.ask ? `<div class="ask">${esc(r.ask)}</div>` : ''}
        </div>` : ''}
        ${err}
      </div>
      <div class="meta">${esc(r.company_query)}<br>plausible ${prob} · ${pri}
        <button type="button" class="btn ghost approach" data-i="${i}">How to approach</button>
        <div>${fbButtons('person', i, r.feedback)}</div>
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
  bindFeedback(data.results, (r) => r.linkedin_url, (r) => r.linkedin_url, (r) => r.name);
}

function downloadCsv() {
  const cell = (v) => `"${String(v ?? '').replace(/"/g, '""')}"`;
  let header, lines, name;

  if (mode === 'jobs') {
    name = 'openings.csv';
    header = ['fit_score', 'title', 'company', 'source', 'url', 'location', 'work_mode', 'pay', 'posted_at', 'closes_at',
              'first_seen', 'level_fit', 'must_haves_met', 'skills_matched', 'skills_missing',
              'why', 'gap', 'verified', 'note'];
    lines = lastRun.results.map((r) => [
      r.fit_score ?? '', r.title, r.company, r.source, r.apply_url || r.url, r.location, r.work_mode || '', r.pay || '', r.posted_at ?? '',
      r.closes_at ?? '', r.first_seen ?? '', r.level_fit ?? '',
      r.must_haves_met == null ? '' : r.must_haves_met.toFixed(3),
      (r.skills_matched || []).join('; '), (r.skills_missing || []).join('; '),
      r.why ?? '', r.gap ?? '', r.verified ? 'yes' : 'no', r.error ?? '',
    ].map(cell).join(','));
  } else {
    name = 'contacts.csv';
    header = ['name', 'headline', 'company', 'linkedin_url', 'fit_score', 'plausible_contact',
              'priority', 'alumni', 'reason', 'ask', 'note'];
    lines = lastRun.results.map((r) => [
      r.name, r.headline, r.company_query, r.linkedin_url,
      r.fit_score ?? '', r.plausible_contact == null ? '' : r.plausible_contact.toFixed(3),
      r.priority ?? '', r.alumni ? 'yes' : '', r.reason ?? '', r.ask ?? '', r.error ?? '',
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
  document.querySelectorAll('.approach:not(.people-btn)').forEach((btn) => {
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
            url: r.key || r.url || r.linkedin_url || '',
            opening_title: r.for_opening || '',
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

/* ---------- people for one opening ---------- */
// The best referral is someone on the team that is hiring right now, so this
// starts from a posting: its recruiter, the team's manager and engineers, and
// alumni of the seeker's college. Drafts from here name the role itself.
let peopleCtx = null;

function paintPeople() {
  const { opening, data } = peopleCtx;
  const rows = data.results.map((c, i) => `<div class="person">
      <div class="person-top">
        <div>
          <a class="name" href="${esc(c.linkedin_url)}" target="_blank" rel="noopener noreferrer nofollow">${esc(c.name)}</a>
          ${c.alumni ? '<span class="badge alum">alumni</span>' : ''}
          ${c.location_match ? '<span class="badge">nearby</span>' : ''}
        </div>
        <div class="score ${scoreClass(c.fit_score)}">${c.fit_score ?? '—'}<small>FIT</small></div>
      </div>
      <div class="head">${esc(c.headline || c.snippet)}</div>
      ${c.reason ? `<div class="rowlines"><div class="reason">${esc(c.reason)}</div></div>` : ''}
      <div class="foot-actions">
        <button type="button" class="btn ghost approach person-draft" data-i="${i}">Draft a message about this role</button>
        ${fbButtons('person', i, c.feedback)}
      </div>
    </div>`).join('');
  const notes = (data.warnings || []).map((w) => `<div class="note">${esc(w)}</div>`).join('');
  drawer().innerHTML = `<div class="drawer-panel" role="dialog" aria-modal="true">
    <div class="drawer-head">
      <div><h3>People for ${esc(opening.title)}</h3>
        <p class="draft-angle">${esc(opening.company)} · ${esc(data.plan.summary || '')}</p></div>
      <button type="button" class="btn ghost" id="drawerClose">Close</button>
    </div>
    ${notes}
    ${rows ? `<div class="people-list">${rows}</div>` : '<p class="draft-foot">No public profiles matched around this role.</p>'}
    <p class="draft-foot">Alumni and location are read from public search snippets — check before you mention them.</p>
  </div>`;
  drawer().hidden = false;
  $('drawerClose').addEventListener('click', closeDrawer);
  bindFeedback(data.results, (c) => c.linkedin_url, (c) => c.linkedin_url, (c) => c.name);
  drawer().querySelectorAll('.person-draft').forEach((btn) => btn.addEventListener('click', async () => {
    const c = data.results[Number(btn.dataset.i)];
    btn.disabled = true;
    btn.textContent = 'Drafting…';
    try {
      const draft = await api('/api/approach', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          kind: 'person', resume: $('resume').value.trim(), role_target: opening.title,
          name: c.name, headline: c.headline || '', company: opening.company, snippet: c.snippet || '',
          url: c.linkedin_url, opening_title: opening.title, opening_url: opening.apply_url || opening.url,
          opening_key: opening.key || '',
        }),
      });
      paintDraft(c, draft);
      const back = document.createElement('button');
      back.type = 'button';
      back.className = 'btn ghost';
      back.textContent = '← Back to people';
      back.addEventListener('click', paintPeople);
      drawer().querySelector('.drawer-head').appendChild(back);
    } catch (err) {
      banner(err.message);
      btn.disabled = false;
      btn.textContent = 'Draft a message about this role';
    }
  }));
}

function bindPeople() {
  document.querySelectorAll('.people-btn').forEach((btn) => btn.addEventListener('click', async () => {
    const o = (lastRun && lastRun.results || [])[Number(btn.dataset.i)];
    if (!o) return;
    drawer().innerHTML = `<div class="drawer-panel"><p>Finding the people around ${esc(o.title)} at ${esc(o.company)}…</p>
      <p class="draft-foot">Counts as one run.</p></div>`;
    drawer().hidden = false;
    try {
      const data = await api('/api/people-for-opening', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          opening_key: o.key || o.url, title: o.title, company: o.company || '',
          department: o.department || '', location: o.location || '', level: o.level || '',
          resume: $('resume').value.trim(), profile: readProfileForm(),
        }),
      });
      account.runs_used = data.runs_used;
      paintAccount();
      peopleCtx = { opening: o, data };
      paintPeople();
    } catch (err) {
      drawer().innerHTML = `<div class="drawer-panel"><div class="drawer-head"><h3>Could not find people</h3>
        <button type="button" class="btn ghost" id="drawerClose">Close</button></div>
        <div class="note bad">${esc(err.message)}</div></div>`;
      $('drawerClose').addEventListener('click', closeDrawer);
    }
  }));
}

/* ---------- saved searches & alerts ---------- */
async function saveSearch() {
  const btn = $('saveSearch');
  btn.disabled = true;
  try {
    const saved = await api('/api/saved-searches', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        resume: $('resume').value.trim(), profile: readProfileForm(),
        roles: (lastRun && lastRun.plan && lastRun.plan.titles) || [],
        companies: splitList($('companies').value), min_score: 60,
      }),
    });
    btn.textContent = 'Saved — alerts on';
    banner(`Saved “${saved.name}”. Boards are checked daily; new matches appear under Alerts.`, 'good');
  } catch (err) {
    banner(err.message);
    btn.disabled = false;
  }
}

async function refreshAlertCount() {
  try {
    const alerts = await api('/api/alerts');
    const unseen = alerts.filter((a) => !a.seen).length;
    $('alertCount').textContent = unseen;
    $('alertCount').hidden = !unseen;
  } catch { /* the count is a nicety */ }
}

async function showAlerts() {
  $('out').innerHTML = '<div class="empty"><strong>Loading alerts…</strong></div>';
  let alerts, searches;
  try {
    [alerts, searches] = await Promise.all([api('/api/alerts'), api('/api/saved-searches')]);
  } catch (err) {
    $('out').innerHTML = `<div class="note bad">${esc(err.message)}</div>`;
    return;
  }
  const rows = alerts.map((a) => `<div class="alert-row ${a.seen ? '' : 'new'}">
      <div class="score ${scoreClass(a.score)}">${a.score}<small>MATCH</small></div>
      <div>
        <a class="name" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>
        <div class="head">${esc(a.company)}${a.location ? ` · ${esc(a.location)}` : ''} · first seen ${esc(a.first_seen.slice(0, 10))}${a.closed ? ' · <b>closed</b>' : ''}</div>
      </div>
      <div class="meta">${esc(a.search_name)}</div>
    </div>`).join('');
  const saved = searches.map((s) => `<div class="saved"><span>${esc(s.name)}
      <span class="opt">${esc([...s.roles, ...s.companies].slice(0, 4).join(', '))}</span></span>
      <button type="button" class="linkish" data-id="${s.id}">Stop alerts</button></div>`).join('');
  $('out').innerHTML = `
    <div class="results-head"><h2>Alerts</h2></div>
    <p class="plan-line">New postings matching your saved searches, found by the daily check of every
      known job board. Match scores here are a quick similarity check — run a search for the full review.</p>
    ${alerts.length ? `<div class="rows">${rows}</div>` : `<div class="empty"><strong>No alerts yet</strong>
      <p>${searches.length ? 'Nothing new has matched since you saved. Boards are checked daily.'
        : 'Run an openings search, then “Alert me to new matches” to start one.'}</p></div>`}
    ${searches.length ? `<details open><summary>${searches.length} saved search${searches.length === 1 ? '' : 'es'}</summary><div>${saved}</div></details>` : ''}`;
  $('out').querySelectorAll('.saved button').forEach((b) => b.addEventListener('click', async () => {
    await api(`/api/saved-searches/${b.dataset.id}`, { method: 'DELETE' });
    showAlerts();
  }));
  if (alerts.some((a) => !a.seen)) {
    api('/api/alerts/seen', { method: 'POST' }).then(refreshAlertCount).catch(() => {});
  }
}

$('alertsBtn').addEventListener('click', showAlerts);

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
  $('olderToggle').hidden = mode !== 'jobs';
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
    const data = await runInBackground({
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
        include_older: $('includeOlder').checked,
        profile: readProfileForm(),
      }),
    });
    // The first openings run reads the profile if it was never read; show it.
    if (data.profile && !profile) { profile = data.profile; paintProfile(); }
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
loadProfile().catch(() => {});
refreshAlertCount();
