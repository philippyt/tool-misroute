from collections import Counter
from misroute.trial import Trial

REMOVED = "(removed tool)"


def neighbours(trials: list[Trial]) -> dict[str, Counter]:
    """Where a removed tool's requests went. Trials carry condition 'removed:<tool>'."""
    out: dict[str, Counter] = {}
    for t in trials:
        if t.condition.startswith("removed:"):
            dest = REMOVED if t.chosen == t.target else t.chosen or "(none)"
            out.setdefault(t.target, Counter())[dest] += 1
    return out
