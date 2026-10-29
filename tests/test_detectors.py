from misroute.detectors.confusion import directions
from misroute.detectors.hedge import co_calls
from misroute.detectors.nearmiss import from_probs, from_repeats
from misroute.trial import Trial


def t(target, calls, rid="r", rep=0, probs=None):
    return Trial("m", "json", target, rid, "x", calls=calls, rep=rep, probs=probs)


def test_directions_and_hedge():
    ts = [t("a", ["b"]), t("a", ["a"]), t("a", ["a", "b"]), t("b", ["c"])]
    d = directions(ts, "a", "b")
    assert d["a>b"]["wrong"] == 1 / 3
    assert d["a>b"]["hedge"] == 1 / 3
    assert d["b>a"]["leak"] == 1.0
    assert co_calls(ts)[("a", "b")] == 1


def test_near_miss_from_probs():
    ts = [t("a", ["a"], probs={"a": 0.55, "b": 0.45}), t("a", ["a"], probs={"a": 0.99, "b": 0.01})]
    nm = from_probs(ts)
    assert nm[0].tool == "a" and nm[0].rival == "b" and nm[0].score == 0.5


def test_near_miss_from_repeats():
    stable = [t("s", ["s"], rid=f"s{i}", rep=r) for i in range(20) for r in range(2)]
    shaky = [t("a", ["a" if r == 0 else "b"], rid=f"a{i}", rep=r) for i in range(20) for r in range(2)]
    pooled, found = from_repeats(stable + shaky)
    assert 0 < pooled < 1
    assert [m.tool for m in found] == ["a"] and found[0].rival == "b"


def test_reads_before_a_write_are_wrong_not_hedge():
    tr = t("update_meta", ["get_events", "get_comments"])
    assert tr.wrong and not tr.hedge
    assert not co_calls([tr])


def test_read_first_is_not_a_wrong_pick():
    from misroute.modes.json_mode import read_first
    from misroute.server.catalog import Tool

    tools = [Tool("update_meta"), Tool("get_events"), Tool("get_comments")]
    tr = t("update_meta", ["get_events"])
    assert read_first(tr, tools)
    tr.failure = "read_first"
    assert not tr.wrong and tr.error


async def test_provider_refusal_stops_cleanly(tmp_path):
    import pytest
    from misroute.config import ModelConfig
    from misroute.models.base import Budget, BudgetExceeded
    from misroute.store.cache import Metered

    class Refused(Exception):
        status_code = 402

    class Broke:
        name, family = "x", "x"

        async def chat(self, user, tools, system=None):
            raise Refused("out of credits")

    m = Metered(Broke(), ModelConfig(name="x", provider="mock"), Budget(1), tmp_path)
    with pytest.raises(BudgetExceeded):
        await m.chat("hi", [])


def test_unknown_and_removed_tools_are_not_pairs():
    from misroute.detectors.rank import rank
    from misroute.detectors.removal import REMOVED, neighbours

    made_up = t("a", ["a_typo"])
    made_up.failure = "unknown_tool"
    assert not made_up.wrong
    gone = t("a", ["a"])
    gone.condition = "removed:a"
    moved = neighbours([gone])
    assert moved == {"a": {REMOVED: 1}}
    assert rank([made_up], moved, set()) == []
