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
  jev_client.py   Jev System One client + fit-score combination
  schemas.py      Pydantic request/response models
frontend/
  index.html      Landing page
  login.html      Sign in / sign up
  app.html        The scout UI
  static/theme.css, static/app.js
```

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
- Resume text is sent to OpenRouter and TypeSafe for scoring, and it isn't stored
  server-side beyond the request. Say so in your privacy policy if you deploy this
  for other people.
- Jev's scoring is a heuristic and can be wrong, especially on thin search snippets.

## Known limitations / TODO

- **Jev's schema is early-access and unverified here** (only relevant with
  `SCORING_PROVIDER=typesafe`). The request shape follows the
  documented System One endpoint; response parsing is deliberately tolerant (bare
  probability, or `{value, probability/confidence}`) and an unrecognized shape surfaces as
  a per-candidate error rather than crashing the run. Check
  [docs.typesafe.ai](https://docs.typesafe.ai/concepts/system-one) if calls start failing.
- No `.docx` resume parsing yet (PDF, PNG, JPG, WebP only).
- Results aren't persisted — each run lives in the browser until you export it.
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
