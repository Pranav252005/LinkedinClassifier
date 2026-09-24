"""Turn one result into something you can actually send.

A ranked list tells you who to talk to; it does not tell you what to say, and
"I would love to connect" is why most outreach is ignored. This drafts the
message for a single person or posting on demand -- after the run, only for the
one the seeker clicked, so a 40-result run does not pay for 40 drafts nobody
reads.

The draft is grounded in two things only: what the resume actually says, and
what the search result actually says. The model is told in the prompt not to
invent shared history, because a fabricated "we met at X" is worse than no
message at all.
"""

from __future__ import annotations

from openrouter import OpenRouterError, chat, parse_json

MAX_POINTS = 4

SYSTEM = (
    "You write short, specific outreach for a job seeker. You never invent facts: "
    "if the seeker's resume does not say it, you do not claim it. You return only "
    "JSON matching the requested shape."
)

_PERSON_RULES = """Write a LinkedIn connection note and a follow-up message.

Rules:
- The note must fit in 280 characters. LinkedIn rejects longer ones.
- Open with the specific overlap you can actually evidence from the resume and
  the profile text: same school, same team, same stack, same past employer.
  If there is no real overlap, say plainly why you are writing instead of
  manufacturing one.
- The ask must be something they can do in under two minutes: a question about
  the team, a pointer to the right recruiter, a referral if and only if the
  overlap genuinely supports asking.
- No flattery, no "I hope this finds you well", no "I am passionate about".
- Never claim to have met them, used a product you cannot evidence, or read
  something they wrote unless the supplied text says so."""

_OPENING_RULES = """Write what to say when applying to this posting.

Rules:
- "talking_points" are the specific things in this seeker's background that map
  onto this posting, quoted close to how the resume states them.
- "gap" names the single most likely objection a screener would raise, and how
  to address it honestly in one line. Do not pretend there is no gap.
- "message" is a short note to a recruiter or hiring manager about this role,
  under 900 characters, specific enough that it could not be sent to any other
  company.
- No flattery, no filler, no invented experience."""

_SHAPE = """Return JSON exactly:
{"headline": "<one line: the angle you are taking>",
 "connection_note": "<<=280 chars, or empty string if not applicable>",
 "message": "<the longer message>",
 "talking_points": ["<specific point>", "..."],
 "gap": "<the honest objection and how to answer it, or empty string>"}"""


def _clip(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


async def draft(kind: str, target: dict, resume: str, role_target: str = "") -> dict:
    """Draft an approach for one person or posting. Raises OpenRouterError."""
    rules = _PERSON_RULES if kind == "person" else _OPENING_RULES

    if kind == "person":
        about = (
            f"Name: {_clip(target.get('name'), 120)}\n"
            f"Headline: {_clip(target.get('headline'), 300)}\n"
            f"Company: {_clip(target.get('company'), 120)}\n"
            f"What the search result says about them: {_clip(target.get('snippet'), 600)}"
        )
        if target.get("opening_title"):
            # Found through a specific opening: the message is about that role.
            about += (
                f"\n\nTHE SPECIFIC OPENING this is about: {_clip(target.get('opening_title'), 200)}"
                f"{' — ' + _clip(target.get('opening_url'), 300) if target.get('opening_url') else ''}\n"
                "Name this role in the note and the message. Ask about it, or for a referral to it, "
                "rather than asking whether there are any openings."
            )
            if target.get("description"):
                about += f"\nWhat the posting asks for: {_clip(target.get('description'), 2500)}"
    else:
        about = (
            f"Role: {_clip(target.get('title'), 200)}\n"
            f"Company: {_clip(target.get('company'), 120)}\n"
            f"Posted: {_clip(target.get('posted_at'), 40) or 'unknown'}\n"
            f"What the posting says: {_clip(target.get('description') or target.get('snippet'), 4000)}"
        )

    prompt = (
        f"{rules}\n\n{_SHAPE}\n\n"
        f"The seeker is targeting: {_clip(role_target, 200) or 'not stated'}\n\n"
        f"--- THE SEEKER'S RESUME ---\n{resume[:6000]}\n\n"
        f"--- WHO/WHAT THEY ARE APPROACHING ---\n{about}\n"
    )

    # Warmer than the planning calls: this is prose, and 0.2 makes every draft
    # read like the last one.
    raw = await chat(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=1100,
        thinking="low",
        temperature=0.6,
    )

    data = parse_json(raw)
    if not isinstance(data, dict):
        raise OpenRouterError("The model did not return an outreach object.")

    points = data.get("talking_points")
    if not isinstance(points, list):
        points = []

    return {
        "headline": _clip(data.get("headline"), 200),
        # Clipped, not rejected: a model that runs long should not cost the user
        # the whole draft, and LinkedIn would truncate it anyway.
        "connection_note": _clip(data.get("connection_note"), 280),
        "message": _clip(data.get("message"), 2000),
        "talking_points": [_clip(p, 300) for p in points if str(p or "").strip()][:MAX_POINTS],
        "gap": _clip(data.get("gap"), 400),
    }
