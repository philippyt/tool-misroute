import asyncio
import duckdb
import pytest
from misroute.config import Config, ModelConfig
from misroute.models.base import (
    NO_LIMIT_ABOVE,
    OUTPUT_ROOM,
    Budget,
    BudgetExceeded,
    Call,
    ProviderRefused,
    Turn,
    until_whole,
)
from misroute.project import Project
from misroute.server.catalog import Tool
from misroute.store.cache import Metered
from misroute.trial import Trial


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


async def test_unknown_price_refuses_before_any_call(tmp_path):
    called = []

    class Spy(Fake):
        async def chat(self, user, tools, system=None):
            called.append(user)
            return await super().chat(user, tools, system)

    cfg = ModelConfig(name="astra", provider="openai-compatible", model="no-such-model", base_url="https://x.invalid")
    m = Metered(Spy(1), cfg, Budget(100), tmp_path)
    with pytest.raises(BudgetExceeded, match="no price known for astra"):
        await m.chat("hi", [])
    assert not called


def project(tmp_path, limit):
    data = {
        "server": {"tools": "unused"},
        "models": [{"name": "m", "provider": "mock", "price_in": 1000, "price_out": 1000}],
        "budget_usd": limit,
    }
    proj = Project(Config.model_validate(data), root=tmp_path)
    proj._tools = [Tool("get_case", "Returns a case.")]
    return proj


async def test_budget_stop_keeps_finished_trials_and_finishes_calls_in_flight(tmp_path):
    proj = project(tmp_path, 0.5)
    trials = [Trial("m", "json", "get_case", f"r{i}", f"show case {i}", phase="discover") for i in range(2000)]
    with pytest.raises(BudgetExceeded, match="saved as discover-partial") as stop:
        await proj.run_trials(trials, lambda t: proj._tools)
    assert " of 2000 trials finished" in str(stop.value)
    assert proj.budget.spent <= 0.5 and proj.budget.reserved == pytest.approx(0)
    with duckdb.connect(str(proj.store.db_path), read_only=True) as con:
        saved = con.execute("select count(*) from trials where phase = 'discover partial'").fetchone()[0]
    assert 0 < saved < 2000
    assert str(saved) in str(stop.value)


async def test_a_rerun_with_more_budget_reuses_finished_calls(tmp_path):
    proj = project(tmp_path, 0.5)
    trials = [Trial("m", "json", "get_case", f"r{i}", f"show case {i}", phase="discover") for i in range(2000)]
    with pytest.raises(BudgetExceeded):
        await proj.run_trials(trials, lambda t: proj._tools)
    again = project(tmp_path, 1000)
    trials = [Trial("m", "json", "get_case", f"r{i}", f"show case {i}", phase="discover") for i in range(2000)]
    done = await again.run_trials(trials, lambda t: again._tools)
    assert len(done) == 2000 and again.adapter("m").hits > 0


async def test_provider_refusal_is_not_treated_as_a_budget_stop(tmp_path):
    class Refused(Exception):
        status_code = 401

    class NoKey:
        name, family = "m", "x"

        async def chat(self, user, tools, system=None):
            raise Refused("bad key")

    proj = project(tmp_path, 5)
    proj._adapters["m"] = Metered(NoKey(), proj.cfg.model("m"), proj.budget, tmp_path)
    with pytest.raises(ProviderRefused):
        await proj.run_trials([Trial("m", "json", "get_case", "r", "x", phase="discover")], lambda t: proj._tools)


def test_cap_counts_only_new_spend_and_never_raises_the_budget(tmp_path):
    proj = project(tmp_path, 5)
    proj.budget.spent = 2.0
    proj.cap(1.0)
    assert proj.budget.limit == 3.0
    proj.cap(100.0)
    assert proj.budget.limit == 5.0
