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
