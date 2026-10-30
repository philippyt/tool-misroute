import itertools
import math
from collections import defaultdict
import numpy as np
from scipy import stats

ALPHA = 0.05
FLAG_MIN_ERRORS = 6
EPS = 1e-12


def min_signflip(alpha: float = ALPHA, sides: int = 2) -> int:
    """Smallest number of nonzero differences for which an exact sign flip test can reach p below alpha."""
    n = 1
    while sides * 0.5**n >= alpha:
        n += 1
    return n


def mc_draws(alpha: float = ALPHA) -> int:
    """Monte Carlo draws that put the standard error of an estimate at alpha to a tenth of alpha."""
    return math.ceil(alpha * (1 - alpha) / (alpha / 10) ** 2)


MIN_DISCORDANT_TEMPLATES = min_signflip(ALPHA, 2)
MIN_PAIRS_SIGNFLIP = min_signflip(ALPHA, 1)
BOOTSTRAP = mc_draws(ALPHA)


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


def tail(values: np.ndarray, probs: np.ndarray, obs: float, alternative: str) -> float:
    if alternative == "greater":
        return float(probs[values >= obs - EPS].sum())
    if alternative == "less":
        return float(probs[values <= obs + EPS].sum())
    return float(probs[np.abs(values) >= abs(obs) - EPS].sum())


def signflip(d: list[float], alternative: str = "less") -> float:
    """Exact sign flip test on the sum of d. Integer differences, such as error counts, use the exact null
    distribution for any n. Other values are enumerated, or sampled when enumeration does not fit in memory."""
    arr = np.asarray(d, float)
    if not len(arr):
        return 1.0
    obs = arr.sum()
    if np.all(arr == np.round(arr)):
        mags = np.abs(arr).astype(int)
        total = int(mags.sum())
        dist = np.zeros(total + 1)
        dist[0] = 1.0
        for m in mags:
            if m:
                nxt = 0.5 * dist
                nxt[m:] += 0.5 * dist[: total + 1 - m]
                dist = nxt
        sums = 2 * np.arange(total + 1) - total
        return tail(sums.astype(float), dist, obs, alternative)
    try:
        signs = np.array(list(itertools.product([1, -1], repeat=len(arr))))
        null = signs @ arr
        return tail(null, np.full(len(null), 1 / len(null)), obs, alternative)
    except MemoryError:
        signs = np.random.default_rng(0).choice([1, -1], size=(mc_draws(), len(arr)))
        null = signs @ arr
        return tail(null, np.full(len(null), 1 / len(null)), obs, alternative)


def binom_greater(k: int, n: int, p: float) -> float:
    if n == 0:
        return 1.0
    return float(stats.binomtest(k, n, min(max(p, 1e-9), 1 - 1e-9), alternative="greater").pvalue)


def midp(fixed: int, broke: int) -> float:
    """Two sided mid p McNemar, better calibrated than the exact test at small counts."""
    n = fixed + broke
    if n == 0:
        return 1.0
    k = min(fixed, broke)
    dist = stats.binom(n, 0.5)
    return float(min(1.0, 2 * (dist.cdf(k) - 0.5 * dist.pmf(k))))


def by_template(base: dict, treat: dict) -> dict[tuple, list[int]]:
    """Errors per request template, summed over slots, orders and repeats. Keys are trial keys whose first three
    parts are model, mode and template."""
    groups: dict[tuple, list[int]] = defaultdict(lambda: [0, 0, 0])
    for k in base.keys() & treat.keys():
        g = groups[k[:3]]
        g[0] += int(base[k])
        g[1] += int(treat[k])
        g[2] += 1
    return dict(groups)


def template_test(base: dict, treat: dict, seed: int = 0) -> dict:
    """Paired comparison with the request template as the unit, since trials from one template fail together.
    The verdict uses an exact sign flip on per template differences. Trial level mid p McNemar, the error rate
    difference with a bootstrap 95% CI over templates, and fixed and broke trial counts are reported alongside."""
    groups = list(by_template(base, treat).values())
    d: list[float] = [float(t - b) for b, t, _ in groups]
    nonzero = [x for x in d if x]
    fixed, broke, _ = mcnemar(base, treat)
    n = sum(g[2] for g in groups) or 1
    diff = sum(d) / n
    ci = [diff, diff]
    if groups:
        rng = np.random.default_rng(seed)
        arr = np.array(groups, float)
        idx = rng.integers(0, len(arr), size=(BOOTSTRAP, len(arr)))
        sample = arr[idx]
        boot = (sample[:, :, 1].sum(1) - sample[:, :, 0].sum(1)) / np.maximum(sample[:, :, 2].sum(1), 1)
        ci = [float(np.percentile(boot, 2.5)), float(np.percentile(boot, 97.5))]
    return {
        "templates": len(groups),
        "discordant_templates": len(nonzero),
        "p": signflip(nonzero, "two-sided") if nonzero else 1.0,
        "p_trial_midp": midp(fixed, broke),
        "fixed": fixed,
        "broke": broke,
        "diff": diff,
        "ci": ci,
    }
