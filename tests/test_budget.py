import asyncio
import pytest
from misroute.config import ModelConfig
from misroute.models.base import (
    NO_LIMIT_ABOVE,
    OUTPUT_ROOM,
    Budget,
    BudgetExceeded,
    Call,
    Turn,
    until_whole,
)
from misroute.store.cache import Metered


class Fake:
    name, family = "m", "x"

    def __init__(self, tokens):
        self.tokens = tokens

    async def chat(self, user, tools, system=None):
        await asyncio.sleep(0)
        room = OUTPUT_ROOM.get()
        used = self.tokens if room is None else min(self.tokens, room)
        return Turn([Call("t", {}, "1")], "", 1000, used, truncated=used < self.tokens)


class Broke:
    name, family = "x", "x"

    async def chat(self, user, tools, system=None):
        raise ValueError("bad")


def metered(adapter, limit, root):
    cfg = ModelConfig(name="m", provider="mock", price_in=5, price_out=5)
    budget = Budget(limit)
    return Metered(adapter, cfg, budget, root), budget


async def batch(root, limit, n=1000, tokens=1000):
    m, budget = metered(Fake(tokens), limit, root)
    found = await asyncio.gather(*(m.chat(f"req {i}", []) for i in range(n)), return_exceptions=True)
    return m, budget, [f for f in found if isinstance(f, BudgetExceeded)]


async def test_a_batch_cannot_spend_past_the_limit(tmp_path):
    m, budget, stopped = await batch(tmp_path, 1.0)
    assert stopped and m.calls < 1000
    assert 0.9 < budget.spent <= 1.0
    assert budget.reserved == pytest.approx(0)


async def test_a_reply_cut_by_the_budget_stops_the_run_and_is_not_cached(tmp_path):
    _, budget, stopped = await batch(tmp_path, 0.02, n=1, tokens=100000)
    assert stopped and "mid reply" in str(stopped[0])
    assert budget.spent <= 0.02
    assert not list((tmp_path / "cache" / "m").glob("*.json"))


async def test_failed_call_gives_its_reservation_back(tmp_path):
    m, budget = metered(Broke(), 1, tmp_path)
    with pytest.raises(ValueError):
        await m.chat("hi", [])
    assert budget.reserved == 0 and budget.spent == 0


async def test_every_truncated_attempt_is_billed():
    seen = []

    async def call(limit):
        seen.append(limit)
        return Turn(text="x", tokens_in=5, tokens_out=limit or 10, truncated=limit != 20)

    turn = await until_whole(call, None)
    assert seen == [None, 20]
    assert turn.tokens_in == 10 and turn.tokens_out == 30 and not turn.truncated


async def test_near_the_limit_output_is_capped_and_retries_stop():
    seen = []

    async def call(limit):
        seen.append(limit)
        return Turn(text="x", tokens_in=5, tokens_out=limit, truncated=True)

    token = OUTPUT_ROOM.set(100)
    try:
        turn = await until_whole(call, None)
    finally:
        OUTPUT_ROOM.reset(token)
    assert seen == [100] and turn.truncated


async def test_far_from_the_limit_no_token_limit_is_sent():
    seen = []

    async def call(limit):
        seen.append(limit)
        return Turn(text="x")

    token = OUTPUT_ROOM.set(NO_LIMIT_ABOVE)
    try:
        await until_whole(call, None)
    finally:
        OUTPUT_ROOM.reset(token)
    assert seen == [None]
