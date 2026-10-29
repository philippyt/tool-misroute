from collections import Counter, defaultdict
from dataclasses import dataclass
from misroute.fix.stats import binom_greater, holm
from misroute.trial import Trial


@dataclass
class NearMiss:
    tool: str
    rival: str | None
    score: float
    p: float | None = None
    source: str = "probs"


def from_probs(trials: list[Trial], margin: float = 0.5) -> list[NearMiss]:
    """Local models: correct trials whose probability for the target beats the best rival by less than margin."""
    by_tool: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for t in trials:
        if not t.probs or t.chosen != t.target:
            continue
        rival, p_rival = max(((k, v) for k, v in t.probs.items() if k != t.target), key=lambda kv: kv[1])
        by_tool[t.target].append((t.probs.get(t.target, 0) - p_rival, rival))
    out = []
    for tool, rows in by_tool.items():
        close = [r for r in rows if r[0] < margin]
        if close:
            rival = Counter(r[1] for r in close).most_common(1)[0][0]
            out.append(NearMiss(tool, rival, len(close) / len(rows)))
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
    return pooled, sorted([m for m in out if (m.p or 1) < 0.05], key=lambda m: -m.score)
