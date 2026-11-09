import asyncio
import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any
from misroute.config import ModelConfig
from misroute.models.base import (
    NO_LIMIT_ABOVE,
    OUTPUT_ROOM,
    Adapter,
    Budget,
    BudgetExceeded,
    Messages,
    ProviderRefused,
    Turn,
    estimate_tokens,
)

LOCAL = ("http://localhost", "http://127.0.0.1", "http://[::1]")


def fill_price(cfg: ModelConfig) -> bool:
    """Use configured prices, zero for local endpoints, else the public list price. False when still unknown."""
    if cfg.price_in is not None or cfg.price_out is not None or cfg.provider == "mock":
        return True
    if (cfg.base_url or "").startswith(LOCAL):
        cfg.price_in = cfg.price_out = 0.0
        return True
    from misroute.models.presets import price

    found = price(cfg.provider, cfg.model_id)
    if found:
        cfg.price_in, cfg.price_out = found
    return found is not None


def no_price(names: str) -> str:
    return (
        f"no price known for {names}, so the budget cannot be enforced. Set price_in and price_out per million "
        "tokens in misroute.yaml, 0 for a free model"
    )


ESTIMATE_TOKENS_OUT = 256
REASONING_TOKENS_OUT = 1024
OBSERVED_FILES = 200


class Metered:
    """Wraps an adapter with a disk cache, a concurrency limit and budget checks."""

    def __init__(self, adapter: Adapter, cfg: ModelConfig, budget: Budget, root: str | Path):
        self.inner = adapter
        self.cfg = cfg
        self.name = adapter.name
        self.family = adapter.family
        self.budget = budget
        self.dir = Path(root) / "cache" / cfg.name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.sem = asyncio.Semaphore(cfg.concurrency)
        self.price_known = fill_price(cfg)
        self.calls = 0
        self.hits = 0
        self.out_tokens = 0
        self.out_calls = 0
        self.cached_out: int | None = None

    def price(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * (self.cfg.price_in or 0) + tokens_out * (self.cfg.price_out or 0)) / 1e6

    def observed_out(self) -> int | None:
        seen = []
        for path in itertools.islice(self.dir.glob("*.json"), OBSERVED_FILES):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("tokens_out"):
                seen.append(data["tokens_out"])
        return math.ceil(sum(seen) / len(seen)) if seen else None

    def expected_out(self) -> int:
        if self.cfg.max_tokens:
            return self.cfg.max_tokens
        if self.out_calls:
            return math.ceil(self.out_tokens / self.out_calls)
        if self.cached_out is None:
            self.cached_out = self.observed_out() or (
                REASONING_TOKENS_OUT if self.cfg.options.get("reasoning_effort") else ESTIMATE_TOKENS_OUT
            )
        return self.cached_out

    def estimate(self, *parts: Any) -> float:
        return self.price(estimate_tokens(*parts), self.expected_out())

    def _path(self, *parts: Any) -> Path:
        key = json.dumps([self.cfg.model_id, self.cfg.options, *parts], sort_keys=True, default=str)
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    def room(self, held: float, *parts: Any) -> int | None:
        if not self.cfg.price_out:
            return None
        return max(int((held - self.price(estimate_tokens(*parts), 0)) * 1e6 / self.cfg.price_out), 1)

    async def _cached(self, parts: tuple, run, encode, decode, keep=lambda out: True):
        path = self._path(*parts)
        if path.exists():
            self.hits += 1
            return decode(json.loads(path.read_text(encoding="utf-8")))
        if not self.price_known:
            raise BudgetExceeded(no_price(self.cfg.name))
        async with self.sem:
            held = self.budget.reserve(self.estimate(*parts), self.cfg.concurrency)
            room = self.room(held, *parts)
            token = OUTPUT_ROOM.set(room)
            try:
                out, cost = await run()
            except Exception as e:
                self.budget.settle(held)
                if getattr(e, "status_code", None) in (401, 402, 403):
                    raise ProviderRefused(f"{self.name} provider refused the request: {e}") from e
                raise
            finally:
                OUTPUT_ROOM.reset(token)
            self.budget.settle(held, cost)
        self.calls += 1
        data = encode(out)
        empty = data in ("", None) or (isinstance(data, dict) and not data["calls"] and not data["text"])
        whole = keep(out)
        if not whole and room is not None and room < NO_LIMIT_ABOVE:
            raise BudgetExceeded(f"budget {self.budget.limit:.2f} usd reached mid reply, {self.budget.spent:.2f} spent")
        if not empty and whole:
            path.write_text(json.dumps(data), encoding="utf-8")
        return out

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None, rep: int = 0) -> Turn:
        async def run():
            t = await self.inner.chat(user, tools, system)
            self.out_tokens += t.tokens_out
            self.out_calls += 1
            return t, t.cost if t.cost is not None else self.price(t.tokens_in, t.tokens_out)

        return await self._cached(
            ("chat", user, tools, system, rep), run, Turn.to_json, Turn.from_json, keep=lambda t: not t.truncated
        )

    async def complete(self, prompt: str, kind: str | None = None, rep: int | str = 0) -> str:
        cut = [False]

        async def run():
            if (turn := getattr(self.inner, "complete_turn", None)) is not None:
                t: Turn = await turn(prompt)
                cut[0] = t.truncated
                return t.text, t.cost if t.cost is not None else self.price(t.tokens_in, t.tokens_out)
            text = await self.inner.complete(prompt, kind)
            return text, self.price(estimate_tokens(prompt), estimate_tokens(text))

        parts = ("complete", prompt, kind, rep, "whole")
        return await self._cached(parts, run, lambda x: x, lambda x: x, keep=lambda _: not cut[0])

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None:
        async def run():
            return await self.inner.score_names(user, tools), 0.0

        return await self._cached(("score", user, tools), run, lambda x: x, lambda x: x)
