import itertools
import math
import re
import numpy as np
from misroute.server.catalog import Tool
from misroute.trial import Request

ALNUM = re.compile(r"[a-z0-9\xe6\xf8\xe5]")
ALPHA = 0.10
EXACT_MAX = 5000
SAMPLES = 300


def pieces(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if ALNUM.search(w)}


def features(text: str, refs: list[set[str]]) -> list[float]:
    p = pieces(text)
    return [*(len(p & r) for r in refs), len(text), len(text.split())]


def fit_predict(xtr: np.ndarray, ytr: np.ndarray, xte: np.ndarray, iters: int = 25) -> np.ndarray:
    """Standardized L2 logistic regression with C = 1 and a free intercept, fitted by Newton steps."""
    mu, sd = xtr.mean(0), xtr.std(0)
    sd[sd == 0] = 1
    a = np.column_stack([(xtr - mu) / sd, np.ones(len(xtr))])
    reg = np.eye(a.shape[1])
    reg[-1, -1] = 0
    w = np.zeros(a.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-(a @ w)))
        grad = a.T @ (p - ytr) + reg @ w
        hess = (a * (p * (1 - p))[:, None]).T @ a + reg + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.abs(step).max() < 1e-8:
            break
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
    """Permutation p that surface overlap and length cannot tell the two request sets apart (thesis Eq 3.11)."""
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
    if math.comb(n, k) <= EXACT_MAX:
        combos = itertools.combinations(range(n), k)
    else:
        rng = np.random.default_rng(seed)
        combos = (rng.choice(n, k, replace=False) for _ in range(SAMPLES))
    nulls = []
    for c in combos:
        lab = np.ones(n, int)
        lab[list(c)] = 0
        nulls.append(lopo(x, lab[groups], groups))
    return float(np.mean(np.array(nulls) >= real - 1e-12))
