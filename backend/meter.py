"""What one run costs to serve.

Pro is $5 a month for 250 runs, which only works if a run costs well under two
cents. Nothing measured that, so this counts every paid call a run makes --
Serper queries, model tokens, embedding tokens, and OpenRouter's own dollar
figure where it reports one -- and the pipeline stores the total per run.

A context variable carries the meter through the async call tree, so the
clients that do the spending (search, openrouter, embeddings, boards) record
into whichever run is active without it being threaded through every call.
Outside a run, `current()` hands back a throwaway meter and nothing is kept.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field


@dataclass
class Meter:
    serper_calls: int = 0
    board_requests: int = 0
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embed_tokens: int = 0
    cost_usd: float = 0.0
    started: float = field(default_factory=time.monotonic)

    def llm(self, usage: dict | None) -> None:
        self.llm_calls += 1
        if not isinstance(usage, dict):
            return
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self._cost(usage)

    def embed(self, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        self.embed_tokens += int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
        self._cost(usage)

    def _cost(self, usage: dict) -> None:
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self.cost_usd += float(cost)

    def summary(self) -> dict:
        out = asdict(self)
        out.pop("started")
        out["duration_ms"] = int((time.monotonic() - self.started) * 1000)
        out["cost_usd"] = round(self.cost_usd, 6)
        return out


_current: ContextVar[Meter | None] = ContextVar("run_meter", default=None)


def start() -> Meter:
    meter = Meter()
    _current.set(meter)
    return meter


def current() -> Meter:
    return _current.get() or Meter()
