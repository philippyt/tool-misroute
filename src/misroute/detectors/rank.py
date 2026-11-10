from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from misroute.fix.stats import FLAG_MIN_ERRORS, FLAG_MIN_TEMPLATES
from misroute.trial import Trial


@dataclass
class Spot:
    a: str
    b: str
    score: float = 0.0
    errors: int = 0
    templates: int = 0
    flagged: bool = False
    one_wording: bool = False
    write: bool = False
    signals: dict[str, float] = field(default_factory=dict)
    thin: bool = False
    directions: dict[str, int] = field(default_factory=dict)
    where: set[str] = field(default_factory=set)
    counts: dict[str, tuple[int, int, int]] = field(default_factory=dict)

    @property
    def pair(self) -> str:
        return f"{self.a}|{self.b}"

    def to_json(self) -> dict:
        d = asdict(self)
        d["where"] = sorted(self.where)
        d.pop("counts")
        top = max(self.counts, key=lambda k: self.signals.get(k, 0), default=None)
        k, n, m = self.counts.get(top, (0, 0, 0))
        d["ci95"] = list(wilson(k / n * m, m) if n else wilson(0, 0))
        return d


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% interval for k of n, so a score carries how little a thin sample pins it down. Spots pass
    request templates as n, since trials from one template fail together and are not independent evidence."""
    if n == 0:
        return 0.0, 1.0
    p, z2 = k / n, z * z
    mid = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * ((p * (1 - p) / n + z2 / (4 * n * n)) ** 0.5) / (1 + z2 / n)
    return max(0.0, mid - half), min(1.0, mid + half)


def rank(trials: list[Trial], removal: Mapping[str, Mapping[str, int]], writes: set[str]) -> list[Spot]:
    """Pairs by measured confusion. A pair is flagged when one model and mode got at least FLAG_MIN_ERRORS
    requests between the two wrong or hedged, over at least FLAG_MIN_TEMPLATES request templates. The same
    errors from a single template set one_wording instead, since one wording repeated is one observation."""
    spots: dict[tuple[str, str], Spot] = {}

    def spot(a: str, b: str) -> Spot:
        key = (min(a, b), max(a, b))
        if key not in spots:
            spots[key] = Spot(key[0], key[1], write=bool(set(key) & writes))
        return spots[key]

    by_where = defaultdict(list)
    for t in trials:
        by_where[(t.model, t.mode)].append(t)
    cell: dict[tuple[str, str], int] = defaultdict(int)
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (model, mode), ts in by_where.items():
        where = f"{model} {mode}"
        n: dict[str, int] = defaultdict(int)
        asked: dict[str, set[str]] = defaultdict(set)
        wrong: dict[tuple[str, str], int] = defaultdict(int)
        hedged: dict[tuple[str, str], int] = defaultdict(int)
        for t in ts:
            n[t.target] += 1
            asked[t.target].add(t.request_id)
            if t.wrong and t.chosen:
                wrong[(t.target, t.chosen)] += 1
                seen[(spot(t.target, t.chosen).pair, where)].add(t.request_id)
            elif t.hedge:
                for other in set(t.picks) - {t.target}:
                    hedged[(min(t.target, other), max(t.target, other))] += 1
                    seen[(spot(t.target, other).pair, where)].add(t.request_id)
        for (target, chosen), c in wrong.items():
            s = spot(target, chosen)
            if c / n[target] > s.signals.get("wrong", 0):
                s.signals["wrong"] = c / n[target]
                s.counts["wrong"] = (c, n[target], len(asked[target]))
            key = f"{target}>{chosen}"
            s.directions[key] = s.directions.get(key, 0) + c
            cell[(s.pair, where)] += c
            s.where.add(where)
        for (a, b), c in hedged.items():
            s = spot(a, b)
            nn = max(n.get(a, 0) + n.get(b, 0), 1)
            if c / nn > s.signals.get("hedge", 0):
                s.signals["hedge"] = c / nn
                s.counts["hedge"] = (c, nn, len(asked[a]) + len(asked[b]))
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
        s.score = max(s.signals.get("wrong", 0.0), s.signals.get("hedge", 0.0))
        cells = [(c, len(seen[key])) for key, c in cell.items() if key[0] == s.pair]
        s.errors, s.templates = max(cells, default=(0, 0))
        enough = any(c >= FLAG_MIN_ERRORS for c, _ in cells)
        s.flagged = any(c >= FLAG_MIN_ERRORS and m >= FLAG_MIN_TEMPLATES for c, m in cells)
        s.one_wording = enough and not s.flagged
    return sorted(spots.values(), key=lambda s: (-s.score, not s.write, -s.signals.get("removal", 0.0), s.pair))


def lookup_candidates(trials: list[Trial], known: frozenset[str] = frozenset()) -> list[dict]:
    """Tools called before the target was reached, for at least FLAG_MIN_ERRORS trials and FLAG_MIN_TEMPLATES
    different targets. A sibling that is confused with one tool leads to that one tool, a catalog or schema lookup
    leads to many, so these are likely entry points to list under lookup_tools rather than wrong picks."""
    led: dict[str, int] = defaultdict(int)
    targets: dict[str, set[str]] = defaultdict(set)
    for t in trials:
        if not t.reached or t.calls[0] == t.target:
            continue
        for name in set(t.calls[: t.calls.index(t.target)]) - known:
            led[name] += 1
            targets[name].add(t.target)
    rows = [
        {"tool": name, "trials": n, "targets": sorted(targets[name])}
        for name, n in led.items()
        if n >= FLAG_MIN_ERRORS and len(targets[name]) >= FLAG_MIN_TEMPLATES
    ]
    return sorted(rows, key=lambda r: (-r["trials"], r["tool"]))
