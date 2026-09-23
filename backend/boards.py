"""Read job boards straight from the applicant tracking systems that host them.

Searching Google for postings returns whatever it happened to index: stale,
duplicated, closed, and described by a 160-character snippet. The ATS vendors
publish the same boards as public JSON, meant for companies to embed on their
own careers pages. Reading those instead gives every open role at a company,
with the full description, the location, the department and real dates -- and
anything a board no longer lists is, by definition, no longer open.

Every fetcher returns the same `Posting` shape, keyed by (ats, slug, job_id),
which is the dedupe key everywhere downstream.

  greenhouse       boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
  lever            api.lever.co/v0/postings/{slug}?mode=json
  ashby            api.ashbyhq.com/posting-api/job-board/{slug}
  workable         apply.workable.com/api/v1/widget/accounts/{slug}?details=true
  smartrecruiters  api.smartrecruiters.com/v1/companies/{slug}/postings
  recruitee        {slug}.recruitee.com/api/offers/
  workday          {tenant}.{wdN}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs

Workday is unofficial -- it is the JSON behind Workday's own careers UI, not a
published API -- so it is polled gently, searched by role rather than listed in
full, and treated as best-effort. It matters because many Indian employers run
their careers site on it.

Nothing here logs in, and nothing is scraped from HTML pages.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import httpx

import meter

TIMEOUT = 20.0
USER_AGENT = "LeadClassifier/0.3 (+job board reader)"
# Public endpoints, but somebody else's servers: keep concurrency modest.
MAX_CONCURRENT = 6
MAX_DESCRIPTION = 12_000

# SmartRecruiters and Workday list postings without descriptions; details are
# fetched one request per posting, so only for the ones that survive filtering.
DETAIL_ATS = ("smartrecruiters", "workday")

WORKDAY_PAGE = 20          # Workday rejects larger pages
WORKDAY_MAX_PAGES = 5      # at most 100 postings per role search per tenant
SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_MAX_PAGES = 5

ATS_NAMES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee", "workday", "ibm")
# Boards searched per role rather than listed in full: a posting missing from
# one read proves nothing about whether it closed.
SEARCHED_ATS = ("workday", "ibm")
IBM_SEARCH_URL = "https://www-api.ibm.com/search/api/v2"
IBM_PAGE = 100


class BoardError(RuntimeError):
    """A board could not be read (network, not found, unexpected shape)."""


@dataclass
class Posting:
    ats: str
    slug: str
    job_id: str
    title: str
    company: str = ""
    url: str = ""
    apply_url: str = ""
    location: str = ""
    remote: bool | None = None
    department: str = ""
    description: str = ""
    posted_at: str | None = None      # YYYY-MM-DD, from the board itself
    closes_at: str | None = None
    # Workday's list says "Posted 3 Days Ago" rather than a date. Kept apart
    # from posted_at until the detail call returns the real one.
    posted_approx: bool = False
    detail_ref: str = ""              # where to fetch the description, if not inline
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.slug}:{self.job_id}"

    def content_hash(self) -> str:
        text = f"{self.title}\n{self.department}\n{self.location}\n{self.description}"
        return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:16]


# --- text helpers ------------------------------------------------------------
_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|section)[^>]*>", re.IGNORECASE)
_SPACES = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n\s*\n+")


def html_to_text(raw: str | None) -> str:
    """Board descriptions are HTML, and Greenhouse's is HTML-escaped HTML."""
    if not raw:
        return ""
    text = html.unescape(raw)
    if "&lt;" in raw and "<" in text:          # escaped twice over
        text = html.unescape(text)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS.sub("\n\n", text).strip()[:MAX_DESCRIPTION]


def iso_day(value: object) -> str | None:
    """A board timestamp (ISO string, or epoch seconds/ms) as YYYY-MM-DD."""
    if isinstance(value, str):
        value = value.strip()
        if re.match(r"^\d{4}-\d{2}-\d{2}", value):
            return value[:10]
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        seconds = value / 1000 if value > 10_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


_RELATIVE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.IGNORECASE)


def relative_day(text: str | None, today: date | None = None) -> tuple[str | None, bool]:
    """Workday's "Posted 3 Days Ago" as a date. Returns (day, is_lower_bound).

    "Posted 30+ Days Ago" only says it is at least thirty days old, so it comes
    back flagged: callers can drop it as old but must not display it as a date.
    """
    today = today or datetime.now(timezone.utc).date()
    found = _RELATIVE.search(text or "")
    if not found:
        return None, False
    word = found.group(1).lower()
    if word == "today":
        return today.isoformat(), False
    if word == "yesterday":
        return (today - timedelta(days=1)).isoformat(), False
    days = int(found.group(2))
    lower_bound = "+" in found.group(0)
    # "30+ days" means more than thirty: date it a day earlier so an age cut-off
    # at thirty removes it.
    return (today - timedelta(days=days + (1 if lower_bound else 0))).isoformat(), lower_bound


def _truthy_remote(*values: object) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, str) and "remote" in value.lower():
            return True
    if any(isinstance(v, bool) for v in values):
        return False
    return None


# --- per-ATS parsers (pure: payload in, postings out) ------------------------
def parse_greenhouse(slug: str, payload: dict) -> list[Posting]:
    out: list[Posting] = []
    for job in (payload or {}).get("jobs") or []:
        job_id = str(job.get("id") or "").strip()
        title = (job.get("title") or "").strip()
        if not job_id or not title:
            continue
        location = ((job.get("location") or {}).get("name") or "").strip()
        departments = [d.get("name", "") for d in job.get("departments") or [] if isinstance(d, dict)]
        url = f"https://job-boards.greenhouse.io/{slug}/jobs/{job_id}"
        out.append(Posting(
            ats="greenhouse", slug=slug, job_id=job_id, title=title,
            company=(job.get("company_name") or "").strip(),
            # absolute_url is often the employer's own site with ?gh_jid=; the
            # board URL is the one that always opens onto the application form.
            url=url, apply_url=url,
            location=location, remote=_truthy_remote(location),
            department=", ".join(d for d in departments if d)[:120],
            description=html_to_text(job.get("content")),
            posted_at=iso_day(job.get("first_published") or job.get("updated_at")),
            closes_at=iso_day(job.get("application_deadline")),
        ))
    return out


def parse_lever(slug: str, payload: list) -> list[Posting]:
    out: list[Posting] = []
    if not isinstance(payload, list):
        return out
    for job in payload:
        job_id = str(job.get("id") or "").strip()
        title = (job.get("text") or "").strip()
        if not job_id or not title:
            continue
        cats = job.get("categories") or {}
        location = (cats.get("location") or "").strip()
        parts = [job.get("descriptionPlain") or html_to_text(job.get("description"))]
        for block in job.get("lists") or []:
            if isinstance(block, dict):
                parts.append(f"{block.get('text', '')}\n{html_to_text(block.get('content'))}")
        parts.append(job.get("additionalPlain") or "")
        workplace = (job.get("workplaceType") or "").lower()
        out.append(Posting(
            ats="lever", slug=slug, job_id=job_id, title=title,
            url=job.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{job_id}",
            apply_url=job.get("applyUrl") or "",
            location=location,
            remote=True if workplace == "remote" else _truthy_remote(location) if workplace == "" else False,
            department=" / ".join(x for x in (cats.get("department"), cats.get("team")) if x)[:120],
            description="\n\n".join(p.strip() for p in parts if p and p.strip())[:MAX_DESCRIPTION],
            posted_at=iso_day(job.get("createdAt")),
            extra={"commitment": cats.get("commitment") or ""},
        ))
    return out


def parse_ashby(slug: str, payload: dict) -> list[Posting]:
    out: list[Posting] = []
    for job in (payload or {}).get("jobs") or []:
        if job.get("isListed") is False:
            continue
        job_id = str(job.get("id") or "").strip()
        title = (job.get("title") or "").strip()
        if not job_id or not title:
            continue
        locations = [job.get("location") or ""]
        locations += [s.get("location", "") for s in job.get("secondaryLocations") or [] if isinstance(s, dict)]
        location = "; ".join(l for l in locations if l)
        out.append(Posting(
            ats="ashby", slug=slug, job_id=job_id, title=title,
            url=job.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{job_id}",
            apply_url=job.get("applyUrl") or "",
            location=location[:200],
            remote=_truthy_remote(job.get("isRemote"), job.get("workplaceType")),
            # Ashby often repeats the department as the team; say it once.
            department=" / ".join(dict.fromkeys(x for x in (job.get("department"), job.get("team")) if x))[:120],
            description=(job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml")))[:MAX_DESCRIPTION],
            posted_at=iso_day(job.get("publishedAt")),
            extra={"employment_type": job.get("employmentType") or ""},
        ))
    return out


def parse_workable(slug: str, payload: dict) -> list[Posting]:
    out: list[Posting] = []
    company = ((payload or {}).get("name") or "").strip()
    for job in (payload or {}).get("jobs") or []:
        job_id = str(job.get("shortcode") or "").strip()
        title = (job.get("title") or "").strip()
        if not job_id or not title:
            continue
        location = ", ".join(x for x in (job.get("city"), job.get("state"), job.get("country")) if x)
        out.append(Posting(
            ats="workable", slug=slug, job_id=job_id, title=title, company=company,
            url=job.get("url") or f"https://apply.workable.com/j/{job_id}",
            apply_url=job.get("application_url") or "",
            location=location, remote=_truthy_remote(job.get("telecommuting")),
            department=(job.get("department") or "")[:120],
            description=html_to_text(job.get("description")),
            posted_at=iso_day(job.get("published_on") or job.get("created_at")),
            extra={"experience": job.get("experience") or "", "employment_type": job.get("employment_type") or ""},
        ))
    return out


def parse_smartrecruiters(slug: str, payload: dict) -> list[Posting]:
    out: list[Posting] = []
    for job in (payload or {}).get("content") or []:
        job_id = str(job.get("id") or "").strip()
        title = (job.get("name") or "").strip()
        if not job_id or not title:
            continue
        loc = job.get("location") or {}
        location = ", ".join(x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x)
        company = (job.get("company") or {}).get("name") or ""
        out.append(Posting(
            ats="smartrecruiters", slug=slug, job_id=job_id, title=title, company=company,
            url=f"https://jobs.smartrecruiters.com/{slug}/{job_id}",
            apply_url=f"https://jobs.smartrecruiters.com/{slug}/{job_id}",
            location=location, remote=_truthy_remote(loc.get("remote")),
            department=((job.get("department") or {}).get("label") or "")[:120],
            posted_at=iso_day(job.get("releasedDate")),
            detail_ref=job.get("ref") or f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}",
            extra={"experience": (job.get("experienceLevel") or {}).get("id") or ""},
        ))
    return out


def smartrecruiters_description(payload: dict) -> str:
    sections = ((payload or {}).get("jobAd") or {}).get("sections") or {}
    parts = []
    for name in ("companyDescription", "jobDescription", "qualifications", "additionalInformation"):
        block = sections.get(name) or {}
        text = html_to_text(block.get("text"))
        if text:
            parts.append(f"{block.get('title') or ''}\n{text}".strip())
    return "\n\n".join(parts)[:MAX_DESCRIPTION]


def parse_recruitee(slug: str, payload: dict) -> list[Posting]:
    out: list[Posting] = []
    for job in (payload or {}).get("offers") or []:
        if job.get("status") not in (None, "published"):
            continue
        job_id = str(job.get("id") or "").strip()
        title = (job.get("title") or "").strip()
        if not job_id or not title:
            continue
        description = html_to_text(job.get("description"))
        requirements = html_to_text(job.get("requirements"))
        url = job.get("careers_url") or f"https://{slug}.recruitee.com/o/{job.get('slug', '')}"
        out.append(Posting(
            ats="recruitee", slug=slug, job_id=job_id, title=title,
            company=(job.get("company_name") or "").strip(),
            url=url, apply_url=job.get("careers_apply_url") or url,
            location=(job.get("location") or "")[:200],
            remote=_truthy_remote(job.get("remote")),
            department=(job.get("department") or "")[:120],
            description=f"{description}\n\n{requirements}".strip()[:MAX_DESCRIPTION],
            posted_at=iso_day(job.get("published_at") or job.get("created_at")),
            extra={"experience": job.get("experience_code") or ""},
        ))
    return out


def workday_parts(slug: str) -> tuple[str, str, str]:
    """A Workday "slug" is stored as tenant|wdN|site, e.g. nvidia|wd5|NVIDIAExternalCareerSite."""
    tenant, host, site = (slug.split("|") + ["", "", ""])[:3]
    if not (tenant and host and site):
        raise BoardError(f"Malformed Workday board reference: {slug!r}")
    return tenant, host, site


def workday_base(slug: str) -> str:
    tenant, host, site = workday_parts(slug)
    return f"https://{tenant}.{host}.myworkdayjobs.com"


def parse_workday(slug: str, payload: dict, today: date | None = None) -> list[Posting]:
    tenant, host, site = workday_parts(slug)
    base = workday_base(slug)
    out: list[Posting] = []
    for job in (payload or {}).get("jobPostings") or []:
        path = (job.get("externalPath") or "").strip()
        title = (job.get("title") or "").strip()
        if not path or not title:
            continue
        # The requisition id is the stable part; the path also carries a slugged title.
        bullets = [b for b in job.get("bulletFields") or [] if isinstance(b, str)]
        job_id = bullets[0] if bullets else path.rsplit("_", 1)[-1]
        posted, lower_bound = relative_day(job.get("postedOn"), today)
        out.append(Posting(
            ats="workday", slug=slug, job_id=job_id, title=title,
            url=f"{base}/{site}{path}", apply_url=f"{base}/{site}{path}",
            location=(job.get("locationsText") or "")[:200],
            remote=_truthy_remote(job.get("locationsText")),
            posted_at=posted, posted_approx=True,
            detail_ref=f"{base}/wday/cxs/{tenant}/{site}{path}",
            extra={"posted_lower_bound": lower_bound},
        ))
    return out


def apply_workday_detail(posting: Posting, payload: dict) -> None:
    info = (payload or {}).get("jobPostingInfo") or {}
    posting.description = html_to_text(info.get("jobDescription"))
    start = iso_day(info.get("startDate"))
    if start:
        posting.posted_at, posting.posted_approx = start, False
    if info.get("location"):
        posting.location = str(info["location"])[:200]
    org = (payload or {}).get("hiringOrganization") or {}
    if isinstance(org, dict) and org.get("name"):
        posting.company = posting.company or str(org["name"])
    if info.get("canApply") is False or info.get("posted") is False:
        posting.extra["closed"] = True


# --- fetching ----------------------------------------------------------------
def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT, follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


async def _get(client: httpx.AsyncClient, url: str, **kw) -> object:
    meter.current().board_requests += 1
    try:
        resp = await client.get(url, **kw)
    except httpx.HTTPError as exc:
        raise BoardError(f"{url}: {exc}") from exc
    if resp.status_code == 404:
        raise BoardError(f"{url}: board not found")
    if resp.status_code >= 400:
        raise BoardError(f"{url}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise BoardError(f"{url}: not JSON") from exc


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> object:
    meter.current().board_requests += 1
    try:
        resp = await client.post(url, json=body, headers={"Content-Type": "application/json"})
    except httpx.HTTPError as exc:
        raise BoardError(f"{url}: {exc}") from exc
    if resp.status_code >= 400:
        raise BoardError(f"{url}: HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError as exc:
        raise BoardError(f"{url}: not JSON") from exc


def parse_ibm(payload: dict) -> list[Posting]:
    """IBM's own careers search (careers.ibm.com), the API its search page calls."""
    hits = ((payload or {}).get("hits") or {}).get("hits") or [] if isinstance(payload, dict) else []
    out = []
    for hit in hits:
        src = hit.get("_source") or {}
        url = src.get("url") or ""
        job_id = url.rsplit("jobId=", 1)[-1] if "jobId=" in url else hit.get("_id", "")
        title = (src.get("title") or "").strip()
        if not title or not url:
            continue
        mode = (src.get("field_keyword_17") or "").strip()
        out.append(Posting(
            ats="ibm", slug="ibm", job_id=str(job_id), title=title, company="IBM", url=url, apply_url=url,
            location=", ".join(x for x in (src.get("field_keyword_19") or "", mode) if x),
            remote=True if mode.lower() == "remote" else None,
            department=src.get("field_keyword_08") or "",
            description=html_to_text(src.get("description")),
            posted_at=(src.get("dcdate") or "")[:10] or None,
        ))
    return out


async def fetch_board(client: httpx.AsyncClient, ats: str, slug: str,
                      search: list[str] | None = None, where: list[str] | None = None) -> list[Posting]:
    """Every open posting on one board. Raises BoardError.

    `search` only matters for Workday and IBM, which are searched per role
    instead of listed in full: some carry tens of thousands of postings.
    `where` (place names) narrows IBM's search to those places.
    """
    if ats == "greenhouse":
        data = await _get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                          params={"content": "true"})
        return parse_greenhouse(slug, data)
    if ats == "lever":
        data = await _get(client, f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
        return parse_lever(slug, data)
    if ats == "ashby":
        data = await _get(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        return parse_ashby(slug, data)
    if ats == "workable":
        data = await _get(client, f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
                          params={"details": "true"})
        return parse_workable(slug, data)
    if ats == "recruitee":
        data = await _get(client, f"https://{slug}.recruitee.com/api/offers/")
        return parse_recruitee(slug, data)
    if ats == "smartrecruiters":
        out: list[Posting] = []
        for page in range(SMARTRECRUITERS_MAX_PAGES):
            data = await _get(client, f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                              params={"limit": SMARTRECRUITERS_PAGE, "offset": page * SMARTRECRUITERS_PAGE})
            batch = parse_smartrecruiters(slug, data)
            out += batch
            total = int((data or {}).get("totalFound") or 0) if isinstance(data, dict) else 0
            if len(batch) < SMARTRECRUITERS_PAGE or len(out) >= total:
                break
        return out
    if ats == "workday":
        tenant, _host, site = workday_parts(slug)
        url = f"{workday_base(slug)}/wday/cxs/{tenant}/{site}/jobs"
        out, seen = [], set()
        for text in (search or [""])[:4]:
            for page in range(WORKDAY_MAX_PAGES):
                data = await _post(client, url, {"appliedFacets": {}, "limit": WORKDAY_PAGE,
                                                  "offset": page * WORKDAY_PAGE, "searchText": text})
                batch = parse_workday(slug, data)
                for p in batch:
                    if p.key not in seen:
                        seen.add(p.key)
                        out.append(p)
                total = int((data or {}).get("total") or 0) if isinstance(data, dict) else 0
                if len(batch) < WORKDAY_PAGE or (page + 1) * WORKDAY_PAGE >= total:
                    break
                await asyncio.sleep(0.3)      # unofficial endpoint: be gentle
        return out
    if ats == "ibm":
        out, seen = [], set()
        for text in (search or [""])[:4]:
            query: dict = {"must": [{"simple_query_string": {"query": text, "fields": ["title^3", "description"]}}]
                           if text else [{"match_all": {}}]}
            if where:
                # IBM hires worldwide; without this most of the page is other countries.
                query["filter"] = [{"bool": {"should": [
                    {"wildcard": {"field_keyword_19": {"value": f"*{w.lower()}*", "case_insensitive": True}}}
                    for w in where]}}]
            query = {"bool": query}
            data = await _post(client, IBM_SEARCH_URL, {
                "appId": "careers", "scopes": ["careers2"], "query": query, "size": IBM_PAGE,
                "_source": ["url", "title", "description", "dcdate", "field_keyword_08",
                            "field_keyword_17", "field_keyword_19"]})
            for p in parse_ibm(data):
                if p.key not in seen:
                    seen.add(p.key)
                    out.append(p)
        return out
    raise BoardError(f"Unknown ATS {ats!r}")


async def fill_details(postings: list[Posting], limit: int = 40) -> int:
    """Fetch descriptions for postings whose board lists them without one."""
    wanted = [p for p in postings if p.ats in DETAIL_ATS and not p.description and p.detail_ref][:limit]
    if not wanted:
        return 0
    gate = asyncio.Semaphore(4)

    async with _client() as client:
        async def one(p: Posting) -> bool:
            async with gate:
                try:
                    data = await _get(client, p.detail_ref)
                except BoardError:
                    return False
            if p.ats == "workday":
                apply_workday_detail(p, data)
            else:
                p.description = smartrecruiters_description(data)
                if isinstance(data, dict) and data.get("active") is False:
                    p.extra["closed"] = True
            return bool(p.description)

        done = await asyncio.gather(*(one(p) for p in wanted))
    return sum(done)


async def fetch_boards(boards: list[tuple[str, str]], search: list[str] | None = None,
                       where: list[str] | None = None
                       ) -> tuple[dict[tuple[str, str], list[Posting]], dict[tuple[str, str], str]]:
    """Fetch several boards concurrently. Returns ({board: postings}, {board: error})."""
    gate = asyncio.Semaphore(MAX_CONCURRENT)
    results: dict[tuple[str, str], list[Posting]] = {}
    errors: dict[tuple[str, str], str] = {}

    async with _client() as client:
        async def one(ats: str, slug: str) -> None:
            async with gate:
                try:
                    results[(ats, slug)] = await fetch_board(client, ats, slug, search, where)
                except BoardError as exc:
                    errors[(ats, slug)] = str(exc)
                except Exception as exc:          # a malformed board never sinks a run
                    errors[(ats, slug)] = f"{type(exc).__name__}: {exc}"

        await asyncio.gather(*(one(a, s) for a, s in dict.fromkeys(boards)))
    return results, errors
