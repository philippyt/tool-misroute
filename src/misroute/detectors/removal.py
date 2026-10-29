from collections import Counter
from misroute.trial import Trial


def neighbours(trials: list[Trial]) -> dict[str, Counter]:
    """Where a removed tool's requests went. Trials carry condition 'removed:<tool>'."""
    out: dict[str, Counter] = {}
    for t in trials:
        if t.condition.startswith("removed:"):
            out.setdefault(t.target, Counter())[t.chosen or "(none)"] += 1
    return out
