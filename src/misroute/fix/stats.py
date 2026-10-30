import itertools
from collections import defaultdict
import numpy as np
from scipy import stats

FLAG_MIN_ERRORS = 6
MIN_PAIRS_SIGNFLIP = 5
MIN_DISCORDANT_TEMPLATES = 6
BOOTSTRAP = 2000
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
