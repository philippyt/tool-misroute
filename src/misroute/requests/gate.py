import itertools
import math
import re
from concurrent.futures import ProcessPoolExecutor
import numpy as np
from misroute.fix.stats import mc_draws
from misroute.server.catalog import Tool
from misroute.trial import Request

ALNUM = re.compile(r"[a-z0-9\xe6\xf8\xe5]")
ALPHA = 0.10
DRAWS = mc_draws(ALPHA)


def pieces(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if ALNUM.search(w)}


def features(text: str, refs: list[set[str]]) -> list[float]:
    p = pieces(text)
    return [*(len(p & r) for r in refs), len(text), len(text.split())]


def fit_predict(xtr: np.ndarray, ytr: np.ndarray, xte: np.ndarray) -> np.ndarray:
    """Standardized L2 logistic regression with C = 1 and a free intercept, fitted by Newton steps until the step
    stops shrinking."""
    mu, sd = xtr.mean(0), xtr.std(0)
    sd[sd == 0] = 1
    a = np.column_stack([(xtr - mu) / sd, np.ones(len(xtr))])
    reg = np.eye(a.shape[1])
    reg[-1, -1] = 0
    w = np.zeros(a.shape[1])
    last = np.inf
    while True:
        p = 1 / (1 + np.exp(-(a @ w)))
        grad = a.T @ (p - ytr) + reg @ w
        hess = (a * (p * (1 - p))[:, None]).T @ a + reg + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(hess, grad)
        w -= step
        size = np.abs(step).max()
        if size < 1e-8 or size >= last:
            break
        last = size
    b = np.column_stack([(xte - mu) / sd, np.ones(len(xte))])
    return (b @ w > 0).astype(int)


def lopo(x: np.ndarray, y: np.ndarray, groups: np.ndarray) -> float:
    preds = np.zeros_like(y)
    for g in np.unique(groups):
        tr = groups != g
        if len(np.unique(y[tr])) < 2:
            preds[~tr] = 1 - y[~tr]
            continue
        preds[~tr] = fit_predict(x[tr], y[tr], x[~tr])
    return float((preds == y).mean())


def gate(a: Tool, b: Tool, reqs_a: list[Request], reqs_b: list[Request], seed: int = 0) -> float:
    """Permutation p that surface overlap and length cannot tell the two request sets apart (thesis Eq 3.11).
    Exact over every assignment, as in the thesis, when that is no more work than the Monte Carlo draws that
    estimate p at the gate's alpha to a tenth of alpha."""
    refs = [
        pieces(a.name.replace("_", " ")),
        pieces(b.name.replace("_", " ")),
        pieces(a.description),
        pieces(b.description),
    ]
    templates = [*reqs_a, *reqs_b]
    n = len(templates)
    if n < 4 or not reqs_a or not reqs_b:
        return 1.0
    rows, tpl_idx = [], []
    for i, r in enumerate(templates):
        for _, text in r.texts():
            rows.append(features(text, refs))
            tpl_idx.append(i)
    x, groups = np.array(rows, float), np.array(tpl_idx)
    label = np.array([0] * len(reqs_a) + [1] * len(reqs_b))
    real = lopo(x, label[groups], groups)
    k = len(reqs_a)
    if math.comb(n, k) <= DRAWS:
        combos = itertools.combinations(range(n), k)
    else:
        rng = np.random.default_rng(seed)
        combos = (rng.choice(n, k, replace=False) for _ in range(DRAWS))
    nulls = []
    for c in combos:
        lab = np.ones(n, int)
        lab[list(c)] = 0
        nulls.append(lopo(x, lab[groups], groups))
    return float(np.mean(np.array(nulls) >= real - 1e-12))


def _screen(job: tuple) -> tuple[str, str, float]:
    a, b, ra, rb = job
    return a.name, b.name, gate(a, b, ra, rb)


def screen(tools: list[Tool], reqs: dict[str, list[Request]]) -> list[tuple[str, str, float]]:
    """Sampled gate on every tool pair. Returns the pairs whose request sets surface cues can tell apart."""
    jobs = [
        (a, b, reqs.get(a.name, []), reqs.get(b.name, []))
        for i, a in enumerate(tools)
        for b in tools[i + 1 :]
        if reqs.get(a.name) and reqs.get(b.name)
    ]
    with ProcessPoolExecutor() as ex:
        results = list(ex.map(_screen, jobs, chunksize=8))
    return [r for r in results if r[2] <= ALPHA]
