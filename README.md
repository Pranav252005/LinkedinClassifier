# LeadClassifier

Paste or upload your resume. A cheap sourcing agent works out who's worth contacting,
public search results supply candidate LinkedIn profiles, and
[TypeSafe's Jev](https://typesafe.ai) — a "System One" decision model that returns
calibrated typed answers instead of generated text — scores and prioritizes every one.

You get a ranked shortlist: name, headline, LinkedIn URL, a 0–100 fit score, and a CSV export.

Landing page, accounts, a $5/mo plan, and PDF/image resume upload are all included.

## How it works

Jev **cannot browse LinkedIn on its own** — it's a scoring model, not a search agent.
A chat model, conversely, can plan but can't give you a calibrated probability. So each
of the three models does the one job it's good at:

| Stage | File | What it does |
| --- | --- | --- |
| **1. Plan** | `backend/agent.py` | A cheap, fast model on [OpenRouter](https://openrouter.ai) reads your resume and returns a JSON search plan — target companies and the contact titles worth querying. Anything you typed yourself is always kept, and kept first. Its output is validated and clamped; the model is never trusted directly. |
| **2. Source** | `backend/search.py` | Each company × title pair becomes a `site:linkedin.com/in "company" "title"` query through [Serper.dev](https://serper.dev). Results are filtered to real `/in/` profile URLs, canonicalized, and deduped. Nothing logs into LinkedIn and nothing is scraped. |
| **3. Classify** | `backend/scoring.py` | Every candidate is judged on two typed questions — `plausible_contact` (probability) and `priority` (high/medium/low) — by whichever backend `SCORING_PROVIDER` selects. |

The two typed answers combine into the ranking score:

```
fit = round(100 * (0.7 * plausible + 0.3 * priority_weight))    # high 1.0 / medium 0.65 / low 0.3
```

Plausibility dominates — a wrong person is useless however "high priority" they look —
and priority breaks ties between similarly plausible contacts.

Resume upload (`backend/resume.py`) reads PDFs locally with `pypdf`; images (and PDFs
that turn out to be scans) go to a vision model on the same OpenRouter key.

## Setup

```bash
git clone <this-repo> && cd leadclassifier
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`:

| Key | Needed for | Where |
| --- | --- | --- |
| `SERPER_API_KEY` | Sourcing | [serper.dev](https://serper.dev) — free tier |
| `SERPER_MAX_RESULTS` | Results per query cap (optional) | defaults to 10 — free keys reject more |
| `OPENROUTER_API_KEY` | Planning, scoring, image OCR | [openrouter.ai](https://openrouter.ai) |
| `TYPESAFE_API_KEY` | Scoring, only if `SCORING_PROVIDER=typesafe` | [typesafe.ai](https://typesafe.ai) — early access via waitlist |
| `SECRET_KEY` | Session signing | `python -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `DATABASE_URL` | Postgres storage (optional) | [neon.tech](https://neon.tech) — leave empty to use local SQLite |
| `COMP_ACCOUNTS` | Always-Pro logins (optional) | your own email, comma separated |
| `STRIPE_*` | The $5 plan | [dashboard.stripe.com](https://dashboard.stripe.com) — optional |

Missing keys degrade rather than crash: without a scoring key candidates come back
unscored with a warning, without `OPENROUTER_API_KEY` the agent step falls back to your
own companies and titles, and without Stripe the upgrade button simply doesn't appear.

### Choosing a scoring backend

`SCORING_PROVIDER` picks who answers the two typed questions:

| | `openrouter` (default) | `typesafe` |
| --- | --- | --- |
| Needs | `OPENROUTER_API_KEY` | `TYPESAFE_API_KEY` |
| Model | any chat model you like | Jev System One |
| Probabilities | plausible-looking numbers | genuinely calibrated |
| Cost | your OpenRouter credits, batched `SCORING_BATCH_SIZE` candidates per call | TypeSafe's pricing |

**Jev is not available through OpenRouter.** It is served only from TypeSafe's own
API — OpenRouter's catalogue contains no `jev`, `typesafe` or `system-one` model. The
`openrouter` backend therefore *approximates* Jev: it asks a normal chat model for the
same two answers and combines them with the same formula. That ranks candidates
sensibly, but a chat model's `0.83` is a plausible-sounding number rather than a
calibrated one, so the ordering is more trustworthy than the absolute values. Switch to
`SCORING_PROVIDER=typesafe` if you get a Jev key and want the real thing.

Run it:

```bash
cd backend && uvicorn main:app --reload --port 8000
```

Open **http://localhost:8000** — the app serves the landing page, auth, and the scout UI.

### Setting up the $5 plan

1. In Stripe, create a recurring $5/month price; put its ID in `STRIPE_PRICE_ID`.
2. Put your secret key in `STRIPE_SECRET_KEY` (`sk_test_…` while you're testing).
3. Point a webhook at `POST /api/billing/webhook` for these events, and put its signing
   secret in `STRIPE_WEBHOOK_SECRET`:
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`.

Locally: `stripe listen --forward-to localhost:8000/api/billing/webhook`.

**The webhook is the only thing that grants Pro.** Returning to the success URL proves
nothing — anyone can visit it — so the redirect only shows a "confirming" message while
Stripe's signed event does the actual upgrade. A `past_due` subscription drops to Free;
one cancelled at period end keeps Pro until the period actually ends.

### Plans

|  | Free | Pro — $5/mo |
| --- | --- | --- |
| Runs per month | 3 | 250 |
| Max candidates per run | 60 | 200 |
| Saved searches with daily alerts | 1 | 10 |
| Everything else | — | same pipeline |

Quotas are per calendar month, counted in SQLite, and tuned by `FREE_RUNS_PER_MONTH` /
`PRO_RUNS_PER_MONTH`. A run is counted when it completes, including runs that find nothing.

## API

| Route | Method | Auth | Purpose |
| --- | --- | --- | --- |
| `/api/auth/signup`, `/api/auth/login`, `/api/auth/logout` | POST | — | Email + password, bcrypt, signed session cookie |
| `/api/account` | GET | session | Plan, runs used/allowed |
| `/api/resume` | POST | session | Multipart PDF/PNG/JPG → plain text |
| `/api/search` | POST | session | Full pipeline, ranked JSON |
| `/api/search.csv` | POST | session | Same run as a CSV download |
| `/api/openings` | POST | session | Openings mode: live job postings, ranked |
| `/api/openings.csv` | POST | session | Same run as a CSV download |
| `/api/profile` | GET/POST/PUT | session | Read the resume into a structured profile; load or save it |
| `/api/people-for-opening` | POST | session | People around one posting (counts as a run) |
| `/api/approach` | POST | session | Draft outreach for one result (no run spent) |
| `/api/feedback` | POST | session | 👍/👎, applied, messaged, replied on one result |
| `/api/saved-searches` | GET/POST/DELETE | session | Searches the daily poll alerts on |
| `/api/alerts`, `/api/alerts/seen` | GET/POST | session | New matching postings |
| `/api/company-timing` | GET | session | Observed hiring months for a company |
| `/api/admin/stats` | GET | operator | Cost per run, precision@10 |
| `/api/admin/poll` | POST | token/operator | Run the daily poll now |
| `/api/billing/checkout`, `/api/billing/portal` | POST | session | Stripe hosted pages |
| `/api/billing/webhook` | POST | signature | Plan activation/cancellation |
| `/api/health` | GET | — | Which keys are configured |

The browser exports CSV from results already on screen, so "Download CSV" doesn't spend a
second round of API credits.

## Project layout

```
backend/
  main.py         FastAPI app: routes, pages, pipeline orchestration
  config.py       Env-backed settings, plan limits
  db.py           Storage: users, plans, run counts (Postgres or SQLite)
  auth.py         bcrypt + signed session cookies
  billing.py      Stripe Checkout, portal, webhook handling
  agent.py        OpenRouter sourcing agent (resume -> search plan)
  scoring.py      Scoring dispatcher + the OpenRouter backend
  openrouter.py   Shared OpenRouter client (chat + vision)
  resume.py       PDF/image -> text
  search.py       Serper sourcing and profile-URL parsing
  seeker.py       Resume -> structured, editable profile
  registry.py     Company -> ATS board discovery, cached
  boards.py       ATS board readers (7 vendors) -> one Posting shape
  openings.py     Openings pipeline: boards, fallback, dedupe, filters
  filters.py      Hard filters in plain code (level, years, location, age, visa)
  embeddings.py   Stage-one similarity, vectors cached per posting
  ranking.py      Stage-two review of the top N on full descriptions
  weights.py      Default and learned ranking weights
  evaluation.py   precision@10, dead links, offline re-ranking
  people.py       People around one opening; alumni/location signals
  poller.py       Daily poll, closing postings, saved-search alerts
  timing.py       Observed hiring months per company
  meter.py        Cost of each run
  jobs.py         Web-search sourcing, now only for discovery and fallback
  jev_client.py   Jev System One client + fit-score combination
  schemas.py      Pydantic request/response models
frontend/
  index.html      Landing page
  login.html      Sign in / sign up
  app.html        The scout UI
  static/theme.css, static/app.js
scripts/
  poll_boards.py  The daily poll (cron)
  eval.py         Export results to label; quality report
  fit_weights.py  Learn ranking weights from labels
tests/            pytest; ATS parsers run against saved real responses
```

Tests: `pip install -r requirements-dev.txt && python -m pytest tests`. They run on a
throwaway SQLite file, never your `DATABASE_URL`.

## Deploying

**Netlify cannot run the backend.** Netlify Functions are JavaScript, TypeScript and
Go only — there is no Python runtime and no persistent ASGI process. So the deployment
is split:

| Piece | Host | Config |
| --- | --- | --- |
| Static frontend | Netlify | `netlify.toml` |
| FastAPI backend | Render / Fly / Railway / any container host | `render.yaml`, `Dockerfile`, `Procfile` |
| Database | Neon Postgres | `DATABASE_URL` |

Netlify proxies `/api/*`, `/login` and `/app` to the backend, so everything stays on
one origin and the session cookie is first-party — no CORS needed. The proxy target
comes from the `API_ORIGIN` environment variable, written into `_redirects` at build
time by `scripts/build-redirects.sh`, so the backend URL is never committed.

### Order of operations

1. **Backend first** — deploy `render.yaml` as a Render blueprint (or the `Dockerfile`
   anywhere). Set `DATABASE_URL`, `SECRET_KEY`, `SERPER_API_KEY`, `OPENROUTER_API_KEY`
   and `COMP_ACCOUNTS` in the host's dashboard. Never commit `.env`.
2. **Point Netlify at it** — set `API_ORIGIN` to the backend origin and redeploy.
3. **Close the loop** — set `PUBLIC_BASE_URL` on the backend to the Netlify URL. This
   turns on `Secure` session cookies and fixes Stripe's return URLs.

### Rate limits

Signup is the expensive door — each free account is `FREE_RUNS_PER_MONTH` runs of paid
Serper and OpenRouter calls. `SIGNUP_LIMIT` (default 5/hour per IP), `LOGIN_LIMIT`
(20/15min) and `RUN_LIMIT` (10/hour) bound that. They are in-process, so with several
workers the effective limit is limit x workers; move them to Redis if that matters.
Set any to `0` to disable.

Before taking real money:

- Session cookies are `Secure` automatically whenever `PUBLIC_BASE_URL` is HTTPS.
- **Use Postgres in production.** Set `DATABASE_URL` to a Neon (or any Postgres)
  connection string and the app stores users, plans and run counts there. Left empty it
  falls back to a local SQLite file, which is fine for one box but is wiped on redeploy
  on hosts with an ephemeral disk (Render, Fly without a volume), taking accounts and
  Pro status with it. `/api/health` reports which backend is live.
- Signup, login and runs are rate limited per IP (see above). Email verification would
  be the next step up if abuse continues.

## Two modes

**People** finds humans to ask for a referral. **Openings** finds the postings
themselves — and, from any posting, the people around that specific role.

### How openings are found and ranked

Searching Google for postings returns whatever it indexed — stale, duplicated,
closed, and described by a 160-character snippet. So openings come straight from
the job boards' own public JSON instead:

| Stage | File | What it does |
| --- | --- | --- |
| **Profile** | `backend/seeker.py` | The resume is read once into fields — target roles, level, years, skills, locations, remote, sponsorship, college — shown as editable chips before the run. |
| **Resolve** | `backend/registry.py` | Each company → its board. Guesses the board name against every ATS API first (free), then one Serper search as a last resort. Found boards are cached in `company_boards`, and misses are cached for 14 days. |
| **Read** | `backend/boards.py` | Every open posting on Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee and (unofficial, best-effort, searched per role) Workday — full description, location, department, real dates. Keyed `(ats, board, job_id)`, so duplicates collapse. |
| **Fallback** | `backend/openings.py` | Companies with no readable board (in-house portals) fall back to web search; every such link is fetched and dropped if dead or closed. Marked "web result" in the UI. |
| **Filter** | `backend/filters.py` | Plain-code rules: level from the title, required years from the description, location, sponsorship, age. Nothing when a posting doesn't say. Every drop is counted by reason and shown ("1695 not internships; 13 outside your locations"). |
| **Rank** | `backend/embeddings.py`, `backend/ranking.py` | Embeddings rank everything left (title and description blended). The top 20 go to the model **with the full description**, which answers narrow questions: must-haves met, level fit (under/fit/over), skills matched, skills missing. |

Each card shows why it ranked where it did: *Matches: Python, Go · Missing:
Kubernetes · Level: fits*. A "quick match" badge means similarity only.

Age: web results older than 30 days are dropped (a stale search hit is usually
filled). A posting the board **still lists** is open by definition, so board
postings get a 120-day limit and older ones simply rank lower —
internship listings routinely stay up for months.

**People for this role** starts from one opening and searches for its recruiter,
the team's manager and engineers, and alumni of your college; alumni and people
in the posting's city rank higher, and the drafted message names the role.

### Measuring quality

Every result has 👍 / 👎 and "applied" / "messaged" / "got a reply". Each label is
stored with the exact features the result was ranked on (`run_results`,
`feedback`), so it stays usable after the posting closes.

```bash
python scripts/eval.py export --runs 10 --out labels.jsonl   # label 150-200 by hand (1/0)
python scripts/eval.py report --labels labels.jsonl --check-links
```

`report` gives precision@10 as shown and re-ranked by the default and learned
weights, plus the dead-link rate of the top ten. Run it before and after every
sourcing or scoring change. `scripts/fit_weights.py` learns the ranking weights
from labels (logistic regression, pure Python) and saves them only past 200
labels **and** only if they beat the defaults on held-out data.

Every run's cost is recorded in `run_costs` (Serper calls, tokens, OpenRouter's
own dollar figure). Operators (`COMP_ACCOUNTS`) see cost per run and quality at
`GET /api/admin/stats`. Measured on a live run: about $0.005 and 10–20 s per
openings run.

### The daily poll, alerts and timing

`scripts/poll_boards.py` (a Render cron job in `render.yaml`, or
`POST /api/admin/poll` with `X-Poll-Token`) re-reads every board in
`company_boards` once a day: new postings get a true first-seen date, and
postings a board stops listing are closed that day. Then every saved search is
matched against what's new, and alerts appear in the app (**Alert me to new
matches** after a run; Free keeps 1 saved search, Pro 10).

Predicting *when* a company posts can't be looked up — boards only show what's
open now. `backend/timing.py` builds it from observed first-seen dates and says
nothing until it has at least 90 days of history; then a card reads e.g.
"Usually posts intern roles in Aug and Jan".

## Storage

| | SQLite (default) | Postgres |
| --- | --- | --- |
| Enabled by | `DATABASE_URL` empty | `DATABASE_URL` set |
| Lives in | `data/app.db` | your Neon project |
| Good for | local dev, single-box self-host | anything deployed |

The schema is created on startup in both cases. Switching backends does not copy
existing rows — accounts made on SQLite do not appear in Postgres.

### Neon

Use the **pooled** connection string (the host contains `-pooler`), which is what
Neon's dashboard gives you by default:

```
DATABASE_URL=postgresql://USER:PASS@ep-xxx-pooler.REGION.aws.neon.tech/neondb?sslmode=require
```

Neon drops idle connections, so the pool is configured small (`max_size=5`) with a
120-second idle recycle, and opens lazily on first use so importing `db.py` never
touches the network. `db.close()` runs on app shutdown to release the pool's threads.

Neon Auth (the `/auth` and JWKS URLs in the Neon dashboard) is a separate product for
outsourcing sign-in. This app has its own email/password auth in `backend/auth.py` and
does not use it.

### Comp accounts

`COMP_ACCOUNTS` is a comma-separated list of emails that are always on Pro without
paying — for the operator's own logins. The grant is declarative rather than a row in
the database, so it survives a database reset, a re-signup with a different password,
and any Stripe event. Those accounts are never shown an upgrade button.

## Responsible use

- This surfaces **public search results**, not a private LinkedIn dataset. It doesn't
  guarantee a profile is current, correctly titled, or still at that company. Treat the
  output as a shortlist to verify, not ground truth about a person.
- **Don't automate LinkedIn connection requests or messages with this.** That breaks
  LinkedIn's terms and gets accounts restricted. There's deliberately no sender here —
  use the ranked list for your own manual outreach.
- Resume text is sent to OpenRouter (and TypeSafe, if selected) for scoring. The raw
  resume isn't stored server-side, but the **structured profile** read from it (roles,
  level, skills, locations, college, graduation year) is, per user, and a saved search
  keeps that profile plus an embedding of the resume so the daily poll can match it.
  Say so in your privacy policy if you deploy this for other people.
- Jev's scoring is a heuristic and can be wrong, especially on thin search snippets.

## Known limitations / TODO

- **Jev's schema is early-access and unverified here** (only relevant with
  `SCORING_PROVIDER=typesafe`). The request shape follows the
  documented System One endpoint; response parsing is deliberately tolerant (bare
  probability, or `{value, probability/confidence}`) and an unrecognized shape surfaces as
  a per-candidate error rather than crashing the run. Check
  [docs.typesafe.ai](https://docs.typesafe.ai/concepts/system-one) if calls start failing.
- No `.docx` resume parsing yet (PDF, PNG, JPG, WebP only).
- Alerts are in-app only; there is no email or push delivery yet.
- Workday boards are searched per role during runs but skipped by the daily poll (a
  tenant can hold tens of thousands of postings), so they get no closing dates.
- Companies on in-house careers portals (common in India) have no board to read; they
  fall back to web search, liveness-checked, snippet-only and undated.
- Hiring-cycle notes need 90+ days of polling before they say anything.
- No password reset flow.
- Search quality depends entirely on what's publicly indexed; smaller and newer companies
  return fewer usable results.
- **Free Serper keys cap results per query at 10.** Asking for more returns
  `400 Query pattern not allowed for free accounts` and fails the whole run, so requests
  are clamped to `SERPER_MAX_RESULTS`. Raise it if you move to a paid plan.
- Queries stop as soon as `max_candidates` is reached. The planner happily produces 90+
  company x title pairs, and running them all would spend the Serper budget on results
  that get discarded.

## License

MIT — see [LICENSE](LICENSE).
