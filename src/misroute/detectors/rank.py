from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from misroute.detectors.confusion import wrong_pairs
from misroute.detectors.hedge import co_calls
from misroute.trial import Trial

WEIGHTS = {"wrong": 1.0, "hedge": 1.0, "removal": 0.25}
WRITE_WEIGHT = 2.0


@dataclass
class Spot:
    a: str
    b: str
    score: float = 0.0
    write: bool = False
    signals: dict[str, float] = field(default_factory=dict)
    gate_failed: bool = False
    where: set[str] = field(default_factory=set)

    @property
    def pair(self) -> str:
        return f"{self.a}|{self.b}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["where"] = sorted(self.where)
        return d


def rank(
    trials: list[Trial],
    removal: Mapping[str, Mapping[str, int]],
    writes: set[str],
    gate_failed: set[tuple[str, str]] | None = None,
) -> list[Spot]:
    spots: dict[tuple[str, str], Spot] = {}

    def spot(a: str, b: str) -> Spot:
        key = (min(a, b), max(a, b))
        if key not in spots:
            spots[key] = Spot(key[0], key[1], write=bool(set(key) & writes))
        return spots[key]

    per_target: dict[tuple, int] = defaultdict(int)
    for t in trials:
        per_target[(t.model, t.mode, t.target)] += 1
    by_where = defaultdict(list)
    for t in trials:
        by_where[(t.model, t.mode)].append(t)
    for (model, mode), ts in by_where.items():
        where = f"{model} {mode}"
        n = {k[2]: v for k, v in per_target.items() if k[:2] == (model, mode)}
        for (target, chosen), c in wrong_pairs(ts).items():
            s = spot(target, chosen)
            s.signals["wrong"] = max(s.signals.get("wrong", 0), c / n[target])
            s.where.add(where)
        for (a, b), c in co_calls(ts).items():
            s = spot(a, b)
            s.signals["hedge"] = max(s.signals.get("hedge", 0), c / max(n.get(a, 0) + n.get(b, 0), 1))
            s.where.add(where)
    for tool, dest in removal.items():
        total = sum(dest.values())
        for other, c in dest.items():
            if other != "(none)" and c / total >= 0.5:
                s = spot(tool, other)
                s.signals["removal"] = c / total
    for s in spots.values():
        s.gate_failed = (s.a, s.b) in (gate_failed or set())
        s.score = sum(WEIGHTS[k] * v for k, v in s.signals.items())
    return sorted(spots.values(), key=lambda s: (-s.score * (WRITE_WEIGHT if s.write else 1), s.pair))
