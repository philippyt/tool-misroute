from collections import Counter, defaultdict
from dataclasses import dataclass
from misroute.fix.stats import ALPHA, binom_greater, holm
from misroute.trial import Trial


@dataclass
class NearMiss:
    tool: str
    rival: str | None
    score: float
    p: float | None = None
    source: str = "probs"


def from_probs(trials: list[Trial]) -> list[NearMiss]:
    """Local models: for correct trials, the share of probability the strongest rival holds against the target,
    averaged per tool. 0 means the rival never had a chance, 1 means a coin flip. Every tool is listed, closest
    first."""
    by_tool: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for t in trials:
        if not t.probs or t.chosen != t.target or len(t.probs) < 2:
            continue
        rival, p_rival = max(((k, v) for k, v in t.probs.items() if k != t.target), key=lambda kv: kv[1])
        p_target = t.probs.get(t.target, 0)
        by_tool[t.target].append((2 * p_rival / (p_rival + p_target) if p_rival + p_target else 0.0, rival))
    out = []
    for tool, rows in by_tool.items():
        rival = Counter(r[1] for r in rows).most_common(1)[0][0]
        out.append(NearMiss(tool, rival, sum(r[0] for r in rows) / len(rows)))
    return sorted(out, key=lambda n: -n.score)


def disagreement(trials: list[Trial]) -> dict[tuple, tuple[int, int, Counter]]:
    groups: dict[tuple, list[Trial]] = defaultdict(list)
    for t in trials:
        groups[(t.model, t.mode, t.request_id, t.slot, t.order)].append(t)
    out = {}
    for k, ts in groups.items():
        if len(ts) < 2:
            continue
        picks = Counter(t.chosen for t in ts)
        out[k] = (len(ts) - picks.most_common(1)[0][1], len(ts), picks)
    return out


def from_repeats(trials: list[Trial]) -> tuple[float, list[NearMiss]]:
    """Hosted models: per tool disagreement across repeats, tested against the pooled retest rate."""
    groups = disagreement(trials)
    if not groups:
        return 0.0, []
    pooled = sum(d for d, _, _ in groups.values()) / sum(n for _, n, _ in groups.values())
    target = {t.request_id: t.target for t in trials}
    per: dict[str, list] = defaultdict(lambda: [0, 0, Counter()])
    for k, (d, n, picks) in groups.items():
        row = per[target[k[2]]]
        row[0] += d
        row[1] += n
        row[2].update(picks)
    tools = list(per)
    ps = holm([binom_greater(per[t][0], per[t][1], pooled) for t in tools])
    out = []
    for tool, p in zip(tools, ps, strict=True):
        d, n, picks = per[tool]
        rivals = [(c, k) for k, c in picks.items() if k not in (tool, None)]
        out.append(NearMiss(tool, max(rivals)[1] if rivals else None, d / n, p, "repeats"))
    return pooled, sorted([m for m in out if (m.p or 1) < ALPHA], key=lambda m: -m.score)
