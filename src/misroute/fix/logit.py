from collections import defaultdict
from collections.abc import Sequence
from typing import Any
import numpy as np
from scipy import stats

ALPHA = 0.05
MIN_CLUSTERS_LOGIT = 30
CLIP = 1e-12
RIDGE = 1e-9


def _sigmoid(eta: np.ndarray) -> np.ndarray:
    eta = np.clip(eta, -35.0, 35.0)
    return 1.0 / (1.0 + np.exp(-eta))


def _loglik(X: np.ndarray, y: np.ndarray, beta: np.ndarray, firth: bool) -> float:
    mu = np.clip(_sigmoid(X @ beta), CLIP, 1.0 - CLIP)
    ll = float(np.sum(y * np.log(mu) + (1.0 - y) * np.log(1.0 - mu)))
    if not firth:
        return ll
    w = mu * (1.0 - mu)
    info = (X.T * w) @ X + RIDGE * np.eye(X.shape[1])
    sign, logdet = np.linalg.slogdet(info)
    return ll + (0.5 * float(logdet) if sign > 0 else -1e12)


def _solve_inv(info: np.ndarray) -> np.ndarray:
    p = info.shape[0]
    try:
        return np.linalg.inv(info)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(info + RIDGE * np.eye(p))


def _fit_irls(
    X: np.ndarray, y: np.ndarray, firth: bool = True, max_iter: int = 50, tol: float = 1e-8
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, bool]:
    """Firth-penalized (or standard MLE) IRLS with step-halving line search."""
    n, p = X.shape
    beta = np.zeros(p, dtype=float)
    p_bar = float(np.clip(np.mean(y), 1e-4, 1.0 - 1e-4))
    if p > 0 and np.allclose(X[:, 0], 1.0):
        beta[0] = float(np.log(p_bar / (1.0 - p_bar)))
    cur_ll = _loglik(X, y, beta, firth)
    converged = False
    inv_info = np.eye(p, dtype=float)
    h = np.zeros(n, dtype=float)
    mu = _sigmoid(X @ beta)
    for _ in range(max_iter):
        mu = np.clip(_sigmoid(X @ beta), CLIP, 1.0 - CLIP)
        w = np.maximum(mu * (1.0 - mu), CLIP)
        info = (X.T * w) @ X + RIDGE * np.eye(p)
        inv_info = _solve_inv(info)
        if firth:
            # Diagonal of hat matrix H = W^{1/2} X I^{-1} X^T W^{1/2}
            h = w * np.sum((X @ inv_info) * X, axis=1)
            score = X.T @ (y - mu + h * (0.5 - mu))
        else:
            h = np.zeros(n, dtype=float)
            score = X.T @ (y - mu)
        step = inv_info @ score
        # Cap step magnitude for stability under separation
        max_step = float(np.max(np.abs(step))) if p else 0.0
        if max_step > 5.0:
            step = step * (5.0 / max_step)
        scale = 1.0
        accepted = False
        for _ in range(12):
            cand = beta + scale * step
            cand_ll = _loglik(X, y, cand, firth)
            if cand_ll >= cur_ll - 1e-10:
                beta = cand
                cur_ll = cand_ll
                accepted = True
                break
            scale *= 0.5
        if not accepted:
            beta = beta + scale * step
            cur_ll = _loglik(X, y, beta, firth)
        if float(np.max(np.abs(scale * step))) < tol:
            converged = True
            break
    mu = np.clip(_sigmoid(X @ beta), CLIP, 1.0 - CLIP)
    w = np.maximum(mu * (1.0 - mu), CLIP)
    info = (X.T * w) @ X + RIDGE * np.eye(p)
    inv_info = _solve_inv(info)
    h = w * np.sum((X @ inv_info) * X, axis=1) if firth else np.zeros(n, dtype=float)
    return beta, mu, inv_info, h, converged


def _cr1_cov(
    X: np.ndarray,
    y: np.ndarray,
    mu: np.ndarray,
    inv_info: np.ndarray,
    h: np.ndarray,
    clusters: Sequence[Any],
    firth: bool,
) -> tuple[np.ndarray, int]:
    """Liang-Zeger CR1 cluster-robust sandwich covariance clustered on template."""
    n, p = X.shape
    resid = (y - mu + h * (0.5 - mu)) if firth else (y - mu)
    scores = X * resid[:, None]
    grouped: dict[Any, np.ndarray] = {}
    for i, g in enumerate(clusters):
        if g in grouped:
            grouped[g] += scores[i]
        else:
            grouped[g] = scores[i].copy()
    G = len(grouped)
    if G <= 1:
        return inv_info, G
    S = np.vstack(list(grouped.values()))
    meat = S.T @ S
    corr = (G / (G - 1.0)) * ((n - 1.0) / max(n - p, 1))
    return corr * (inv_info @ meat @ inv_info), G


def _binary_ame(
    X: np.ndarray, beta: np.ndarray, cov: np.ndarray, col: int
) -> tuple[float, float]:
    """Average marginal effect and delta-method cluster-robust SE for a binary column."""
    X1 = X.copy()
    X0 = X.copy()
    X1[:, col] = 1.0
    X0[:, col] = 0.0
    mu1 = _sigmoid(X1 @ beta)
    mu0 = _sigmoid(X0 @ beta)
    ame = float(np.mean(mu1 - mu0))
    grad = np.mean((mu1 * (1.0 - mu1))[:, None] * X1 - (mu0 * (1.0 - mu0))[:, None] * X0, axis=0)
    var = float(grad @ cov @ grad)
    return ame, float(np.sqrt(max(var, 0.0)))


def clustered_logit(
    X: np.ndarray,
    y: np.ndarray,
    clusters: Sequence[Any],
    names: list[str] | None = None,
    firth: bool = True,
    max_iter: int = 50,
    tol: float = 1e-8,
) -> dict[str, Any]:
    """Clustered Firth-penalized logistic regression with CR1 sandwich SEs and average marginal effects.

    Firth bias reduction prevents divergence under complete separation (such as zero errors in one condition),
    and clustering on request templates accounts for correlated trials from the same template.
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    n, p = X.shape
    names = names or [f"x{i}" for i in range(p)]
    unique_clusters = len(set(clusters))
    if n == 0 or p == 0:
        return {
            "converged": False,
            "n": n,
            "clusters": unique_clusters,
            "underpowered_clusters": True,
            "firth": firth,
            "log_likelihood": 0.0,
            "null_log_likelihood": 0.0,
            "pseudo_r2": 0.0,
            "terms": {},
        }
    p_bar = float(np.clip(np.mean(y), CLIP, 1.0 - CLIP))
    null_ll = float(np.sum(y * np.log(p_bar) + (1.0 - y) * np.log(1.0 - p_bar)))
    beta, mu, inv_info, h, converged = _fit_irls(X, y, firth=firth, max_iter=max_iter, tol=tol)
    ll = float(np.sum(y * np.log(mu) + (1.0 - y) * np.log(1.0 - mu)))
    cov, G = _cr1_cov(X, y, mu, inv_info, h, clusters, firth=firth)
    df = max(G - 1, 1)
    crit = float(stats.t.ppf(1.0 - ALPHA / 2.0, df=df))
    terms: dict[str, dict[str, Any]] = {}
    for j, name in enumerate(names):
        b = float(beta[j])
        se = float(np.sqrt(max(float(cov[j, j]), 0.0)))
        if se > 0:
            z = b / se
            pval = float(2.0 * stats.t.sf(abs(z), df=df))
        else:
            z = 0.0
            pval = 1.0 if abs(b) < 1e-12 else 0.0
        lo = float(np.clip(b - crit * se, -30.0, 30.0))
        hi = float(np.clip(b + crit * se, -30.0, 30.0))
        entry: dict[str, Any] = {
            "coef": b,
            "se": se,
            "z": z,
            "p": pval,
            "odds_ratio": float(np.exp(np.clip(b, -30.0, 30.0))),
            "or_ci": [float(np.exp(lo)), float(np.exp(hi))],
        }
        # Compute average marginal effect for binary non-intercept columns
        vals = set(np.unique(X[:, j]).tolist())
        if j > 0 and vals <= {0.0, 1.0} and len(vals) == 2:
            ame, ame_se = _binary_ame(X, beta, cov, j)
            entry["ame"] = ame
            entry["ame_se"] = ame_se
            entry["ame_ci"] = [ame - crit * ame_se, ame + crit * ame_se]
        terms[name] = entry
    pseudo_r2 = max(0.0, 1.0 - ll / null_ll) if null_ll < -CLIP else 0.0
    return {
        "converged": converged,
        "n": n,
        "clusters": G,
        "underpowered_clusters": G < MIN_CLUSTERS_LOGIT,
        "firth": firth,
        "log_likelihood": ll,
        "null_log_likelihood": null_ll,
        "pseudo_r2": pseudo_r2,
        "terms": terms,
    }


def paired_logit_test(base: dict, treat: dict) -> dict[str, Any]:
    """Paired clustered Firth logistic regression companion to template_test.

    Fits error ~ treatment with trials clustered on their request template (key[:3]). Reported alongside the
    exact template sign flip test so users get odds ratios, average marginal effects and cluster-robust p values.
    """
    keys = sorted(base.keys() & treat.keys())
    if not keys:
        return {
            "clusters": 0,
            "underpowered_clusters": True,
            "coef": 0.0,
            "se": 0.0,
            "p": 1.0,
            "odds_ratio": 1.0,
            "or_ci": [1.0, 1.0],
            "ame": 0.0,
            "ame_ci": [0.0, 0.0],
        }
    rows_x = []
    rows_y = []
    clusters = []
    for k in keys:
        cid = k[:3] if isinstance(k, tuple) and len(k) >= 3 else k
        rows_x.append([1.0, 0.0])
        rows_y.append(float(bool(base[k])))
        clusters.append(cid)
        rows_x.append([1.0, 1.0])
        rows_y.append(float(bool(treat[k])))
        clusters.append(cid)
    X = np.asarray(rows_x, dtype=float)
    y = np.asarray(rows_y, dtype=float)
    G = len(set(clusters))
    if np.all(y == y[0]):
        return {
            "clusters": G,
            "underpowered_clusters": G < MIN_CLUSTERS_LOGIT,
            "coef": 0.0,
            "se": 0.0,
            "p": 1.0,
            "odds_ratio": 1.0,
            "or_ci": [1.0, 1.0],
            "ame": 0.0,
            "ame_ci": [0.0, 0.0],
        }
    fit = clustered_logit(X, y, clusters, names=["intercept", "treatment"], firth=True)
    term = fit["terms"]["treatment"]
    return {
        "clusters": fit["clusters"],
        "underpowered_clusters": fit["underpowered_clusters"],
        "coef": term["coef"],
        "se": term["se"],
        "p": term["p"],
        "odds_ratio": term["odds_ratio"],
        "or_ci": term["or_ci"],
        "ame": term.get("ame", 0.0),
        "ame_ci": term.get("ame_ci", [0.0, 0.0]),
    }


def _design_for_factors(
    trials: Sequence[Any],
    active: list[str],
    model_families: dict[str, str],
) -> tuple[np.ndarray, list[str], dict[str, list[int]]]:
    """Build one-hot design matrix with intercept and return column indices per factor."""
    n = len(trials)
    cols: list[np.ndarray] = [np.ones(n, dtype=float)]
    names: list[str] = ["intercept"]
    factor_cols: dict[str, list[int]] = {}

    def add_cat(factor: str, values: list[str]) -> None:
        levels = sorted(set(values))
        if len(levels) < 2:
            return
        idxs = []
        # Reference level is the first sorted level
        for lvl in levels[1:]:
            idxs.append(len(cols))
            cols.append(np.array([1.0 if v == lvl else 0.0 for v in values], dtype=float))
            names.append(f"{factor}:{lvl}")
        factor_cols[factor] = idxs

    for factor in active:
        if factor == "model":
            add_cat("model", [t.model for t in trials])
        elif factor == "tool":
            add_cat("tool", [t.target for t in trials])
        elif factor == "mode":
            add_cat("mode", [t.mode for t in trials])
        elif factor == "order":
            add_cat("order", [str(t.order) for t in trials])
        elif factor == "same_gen_family":
            vals = []
            for t in trials:
                gfam = getattr(t, "generator_family", "")
                vals.append(1.0 if gfam and model_families.get(t.model) == gfam else 0.0)
            if len(set(vals)) == 2:
                factor_cols["same_gen_family"] = [len(cols)]
                cols.append(np.array(vals, dtype=float))
                names.append("same_gen_family")

    return np.column_stack(cols), names, factor_cols


def _plain_ll(X: np.ndarray, y: np.ndarray) -> float:
    _, mu, _, _, _ = _fit_irls(X, y, firth=True)
    return float(np.sum(y * np.log(mu) + (1.0 - y) * np.log(1.0 - mu)))


def decompose_errors(
    trials: Sequence[Any],
    model_families: dict[str, str] | None = None,
    max_tools: int = 200,
) -> dict[str, Any]:
    """Multi-factor clustered Firth logistic regression decomposing routing errors across tool/description,
    model, mode, tool order and generator-family match.

    Reports adjusted odds ratios and average marginal effects per term, plus a deviance / McFadden pseudo-R2
    decomposition showing how much explained variance belongs to the tool catalog versus the model.
    """
    model_families = model_families or {}
    if not trials:
        return {"n": 0, "clusters": 0, "factors": {}, "terms": {}, "pseudo_r2": 0.0}
    y = np.array([1.0 if t.error else 0.0 for t in trials], dtype=float)
    clusters = [(t.target, t.request_id) for t in trials]
    G = len(set(clusters))
    if np.all(y == y[0]):
        return {
            "n": len(trials),
            "clusters": G,
            "underpowered_clusters": G < MIN_CLUSTERS_LOGIT,
            "factors": {},
            "terms": {},
            "pseudo_r2": 0.0,
        }
    candidates = ["model", "mode", "order", "same_gen_family"]
    unique_tools = {t.target for t in trials}
    if 2 <= len(unique_tools) <= max_tools:
        candidates.insert(0, "tool")
    X, names, factor_cols = _design_for_factors(trials, candidates, model_families)
    if not factor_cols:
        return {
            "n": len(trials),
            "clusters": G,
            "underpowered_clusters": G < MIN_CLUSTERS_LOGIT,
            "factors": {},
            "terms": {},
            "pseudo_r2": 0.0,
        }
    fit = clustered_logit(X, y, clusters, names=names, firth=True)
    full_ll = fit["log_likelihood"]
    null_ll = fit["null_log_likelihood"]
    full_drop = max(0.0, 2.0 * (full_ll - null_ll))

    # Drop-one deviance contribution for each active factor (unique variance explained)
    # and standalone deviance contribution (marginal variance explained)
    factor_summary: dict[str, dict[str, Any]] = {}
    unique_drops: dict[str, float] = {}
    for fac, idxs in factor_cols.items():
        keep = [i for i in range(X.shape[1]) if i not in idxs]
        ll_without = _plain_ll(X[:, keep], y)
        unique_drop = max(0.0, 2.0 * (full_ll - ll_without))
        ll_only = _plain_ll(X[:, [0, *idxs]], y)
        marginal_drop = max(0.0, 2.0 * (ll_only - null_ll))
        unique_drops[fac] = unique_drop
        factor_summary[fac] = {
            "df": len(idxs),
            "deviance_drop": unique_drop,
            "marginal_deviance_drop": marginal_drop,
            "marginal_pseudo_r2": max(0.0, 1.0 - ll_only / null_ll) if null_ll < -CLIP else 0.0,
            "share_of_full_deviance": unique_drop / full_drop if full_drop > CLIP else 0.0,
        }
    total_unique = sum(unique_drops.values())
    for fac, row in factor_summary.items():
        row["share_of_unique_deviance"] = unique_drops[fac] / total_unique if total_unique > CLIP else 0.0

    # Per-model unadjusted error rates for reference alongside adjusted OR/AME
    by_model: dict[str, list[float]] = defaultdict(list)
    for t in trials:
        by_model[t.model].append(1.0 if t.error else 0.0)
    references = {}
    if "model" in factor_cols:
        references["model"] = sorted({t.model for t in trials})[0]
    if "tool" in factor_cols:
        references["tool"] = sorted(unique_tools)[0]
    if "mode" in factor_cols:
        references["mode"] = sorted({t.mode for t in trials})[0]
    if "order" in factor_cols:
        references["order"] = sorted({str(t.order) for t in trials})[0]

    return {
        "n": fit["n"],
        "clusters": fit["clusters"],
        "underpowered_clusters": fit["underpowered_clusters"],
        "converged": fit["converged"],
        "pseudo_r2": fit["pseudo_r2"],
        "null_deviance": -2.0 * null_ll,
        "residual_deviance": -2.0 * full_ll,
        "references": references,
        "factors": factor_summary,
        "terms": {k: v for k, v in fit["terms"].items() if k != "intercept"},
    }
