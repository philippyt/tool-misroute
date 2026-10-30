from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from misroute.detectors.confusion import wrong_pairs
from misroute.detectors.hedge import co_calls
from misroute.fix.stats import FLAG_MIN_ERRORS
from misroute.trial import Trial


@dataclass
class Spot:
    a: str
    b: str
    score: float = 0.0
    errors: int = 0
    flagged: bool = False
    write: bool = False
    signals: dict[str, float] = field(default_factory=dict)
    gate_failed: bool = False
    thin: bool = False
    directions: dict[str, int] = field(default_factory=dict)
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

    cell: dict[tuple[str, str], int] = defaultdict(int)
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
            key = f"{target}>{chosen}"
            s.directions[key] = s.directions.get(key, 0) + c
            cell[(s.pair, where)] += c
            s.where.add(where)
        for (a, b), c in co_calls(ts).items():
            s = spot(a, b)
            s.signals["hedge"] = max(s.signals.get("hedge", 0), c / max(n.get(a, 0) + n.get(b, 0), 1))
            cell[(s.pair, where)] += c
            s.where.add(where)
    for tool, dest in removal.items():
        real = {k: v for k, v in dest.items() if not k.startswith("(")}
        if not real:
            continue
        total, top = sum(dest.values()), max(real.values())
        for other, c in real.items():
            if c == top:
                spot(tool, other).signals["removal"] = c / total
    for s in spots.values():
        s.gate_failed = (s.a, s.b) in (gate_failed or set())
        s.score = max(s.signals.get("wrong", 0.0), s.signals.get("hedge", 0.0))
        s.errors = max((c for (pair, _), c in cell.items() if pair == s.pair), default=0)
        s.flagged = s.errors >= FLAG_MIN_ERRORS
    return sorted(spots.values(), key=lambda s: (-s.score, not s.write, -s.signals.get("removal", 0.0), s.pair))
