import itertools
import numpy as np
from scipy import stats

FLAG_MIN_ERRORS = 6
MIN_PAIRS_SIGNFLIP = 5
RNG = np.random.default_rng(0)


def holm(pvals: list[float]) -> list[float]:
    if not pvals:
        return []
    order = np.argsort(pvals)
    adj = np.empty(len(pvals))
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, (len(pvals) - rank) * pvals[idx])
        adj[idx] = min(1.0, running)
    return adj.tolist()


def mcnemar(a: dict, b: dict) -> tuple[int, int, float]:
    """Exact McNemar on paired error indicators keyed by trial key. Returns fixed, broke, two sided p."""
    keys = a.keys() & b.keys()
    fixed = sum(1 for k in keys if a[k] and not b[k])
    broke = sum(1 for k in keys if not a[k] and b[k])
    p = 1.0 if fixed + broke == 0 else float(stats.binomtest(fixed, fixed + broke, 0.5).pvalue)
    return fixed, broke, p


def signflip(d: list[float], alternative: str = "less") -> float:
    """Exact sign flip test on the mean of d, enumerated up to 20 values, else 200k draws."""
    arr = np.asarray(d, float)
    if not len(arr):
        return 1.0
    obs = arr.mean()
    if len(arr) <= 20:
        signs = np.array(list(itertools.product([1, -1], repeat=len(arr))))
    else:
        signs = RNG.choice([1, -1], size=(200_000, len(arr)))
    null = (signs * arr).mean(1)
    if alternative == "greater":
        return float(np.mean(null >= obs - 1e-12))
    if alternative == "less":
        return float(np.mean(null <= obs + 1e-12))
    return float(np.mean(np.abs(null) >= abs(obs) - 1e-12))


def binom_greater(k: int, n: int, p: float) -> float:
    if n == 0:
        return 1.0
    return float(stats.binomtest(k, n, min(max(p, 1e-9), 1 - 1e-9), alternative="greater").pvalue)
