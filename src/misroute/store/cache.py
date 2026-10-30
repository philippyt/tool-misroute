import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from misroute.config import ModelConfig
from misroute.models.base import Adapter, Budget, BudgetExceeded, Messages, Turn, estimate_tokens

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

    def price(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in * (self.cfg.price_in or 0) + tokens_out * (self.cfg.price_out or 0)) / 1e6

    def estimate(self, *parts: Any) -> float:
        return self.price(estimate_tokens(*parts), self.cfg.max_tokens // 4)

    def _path(self, *parts: Any) -> Path:
        key = json.dumps([self.cfg.model_id, self.cfg.options, *parts], sort_keys=True, default=str)
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".json")

    async def _cached(self, parts: tuple, run, encode, decode):
        path = self._path(*parts)
        if path.exists():
            self.hits += 1
            return decode(json.loads(path.read_text(encoding="utf-8")))
        self.budget.check(self.estimate(*parts))
        async with self.sem:
            try:
                out, cost = await run()
            except Exception as e:
                if getattr(e, "status_code", None) in (401, 402, 403):
                    raise BudgetExceeded(f"{self.name} provider refused the request: {e}") from e
                raise
        self.budget.charge(cost)
        self.calls += 1
        data = encode(out)
        if data not in ("", None) and not (isinstance(data, dict) and not data["calls"] and not data["text"]):
            path.write_text(json.dumps(data), encoding="utf-8")
        return out

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None, rep: int = 0) -> Turn:
        async def run():
            t = await self.inner.chat(user, tools, system)
            return t, t.cost if t.cost is not None else self.price(t.tokens_in, t.tokens_out)

        return await self._cached(("chat", user, tools, system, rep), run, Turn.to_json, Turn.from_json)

    async def complete(self, prompt: str, kind: str | None = None, rep: int = 0) -> str:
        async def run():
            if (turn := getattr(self.inner, "complete_turn", None)) is not None:
                t: Turn = await turn(prompt)
                return t.text, t.cost if t.cost is not None else self.price(t.tokens_in, t.tokens_out)
            text = await self.inner.complete(prompt, kind)
            return text, self.price(estimate_tokens(prompt), estimate_tokens(text))

        return await self._cached(("complete", prompt, kind, rep), run, lambda x: x, lambda x: x)

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None:
        async def run():
            return await self.inner.score_names(user, tools), 0.0

        return await self._cached(("score", user, tools), run, lambda x: x, lambda x: x)
