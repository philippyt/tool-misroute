#!/usr/bin/env python3
"""Run a multi-server, multi-family misroute leaderboard benchmark.

Usage (from repo root):
    uv run python leaderboard/run.py --preset surface --max-cost 7.5
    uv run python leaderboard/run.py --preset surface --mock --out leaderboard/results/mock
"""

from __future__ import annotations
import argparse
import asyncio
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from misroute.config import Config, family, load_env
from misroute.detectors.rank import wilson
from misroute.fix.logit import decompose_errors
from misroute.models.base import Budget, BudgetExceeded
from misroute.project import Project
from misroute.store.manifest import (
    misroute_version,
    sample_for_audit,
    save_requests,
    write_audit_jsonl,
)
from misroute.trial import Trial

LEADERBOARD_DIR = Path(__file__).resolve().parent
SERVERS_DIR = LEADERBOARD_DIR / "servers"

SURFACE_SERVERS = [
    "fetch",
    "time",
    "entur",
    "memory",
    "git",
    "filesystem",
]

# 6 families: budget + open-weight + frontier (surface preset uses budget+open-weight + mid/frontier)
SURFACE_MODELS = [
    "openrouter:anthropic/claude-sonnet-5.5",
    "openrouter:anthropic/claude-haiku-5.5",
    "openrouter:openai/gpt-5.6-terra",
    "openrouter:openai/gpt-5.6-luna",
    "openrouter:google/gemini-4-hydrogen",
    "openrouter:google/gemini-3.8-flash",
    "openrouter:qwen/qwen3.8-flash",
    "openrouter:meta-llama/llama-4-maverick",
    "openrouter:mistralai/mistral-large-2512",
]

SURFACE_GENERATORS = [
    "openrouter:anthropic/claude-haiku-5.5",
    "openrouter:openai/gpt-5.6-luna",
    "openrouter:google/gemini-3.8-flash",
]

SURFACE_VALIDATORS = [
    "openrouter:anthropic/claude-haiku-5.5",
    "openrouter:openai/gpt-5.6-luna",
    "openrouter:google/gemini-3.8-flash",
]


def _mock_models() -> tuple[list[dict[str, Any]], list[str], list[str], list[str]]:
    models = [
        {"name": "claude-sonnet-mock", "provider": "mock", "options": {"family": "anthropic", "prefix": "kindly"}},
        {"name": "gpt-terra-mock", "provider": "mock", "options": {"family": "openai", "prefix": "please"}},
        {"name": "gemini-hydro-mock", "provider": "mock", "options": {"family": "google", "prefix": "could"}},
        {"name": "qwen-mock", "provider": "mock", "options": {"family": "qwen", "prefix": "help"}},
        {"name": "llama-mock", "provider": "mock", "options": {"family": "meta", "prefix": "can"}},
        {"name": "mistral-mock", "provider": "mock", "options": {"family": "mistral", "prefix": "would"}},
    ]
    tested = [m["name"] for m in models]
    gens = ["claude-sonnet-mock", "gpt-terra-mock", "gemini-hydro-mock"]
    vals = ["claude-sonnet-mock", "gpt-terra-mock", "gemini-hydro-mock"]
    return models, tested, gens, vals


def _load_trials_jsonl(run_dir: Path, run_id: str) -> list[Trial]:
    path = run_dir / "runs" / f"{run_id}.jsonl"
    if not path.is_file():
        return []
    out: list[Trial] = []
    valid_fields = set(Trial.__dataclass_fields__)
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("phase") == "removal":
            continue
        out.append(Trial(**{k: v for k, v in row.items() if k in valid_fields}))
    return out


def _template_wilson(trials: list[Trial], pred) -> tuple[float, list[float], int]:
    """Rate over trials with a Wilson 95% CI sized by distinct request templates."""
    if not trials:
        return 0.0, [0.0, 1.0], 0
    rate = sum(1 for t in trials if pred(t)) / len(trials)
    n_tpl = len({(t.target, t.request_id) for t in trials})
    lo, hi = wilson(rate * n_tpl, n_tpl)
    return rate, [lo, hi], n_tpl


def _summarize_subset(trials: list[Trial], writes: set[str]) -> dict[str, Any]:
    if not trials:
        return {"trials": 0, "templates": 0}
    wrong, wrong_ci, n_tpl = _template_wilson(trials, lambda t: t.wrong)
    hedge, hedge_ci, _ = _template_wilson(trials, lambda t: t.hedge)
    w_trials = [t for t in trials if t.target in writes]
    w_hedge, w_hedge_ci, _ = _template_wilson(w_trials, lambda t: t.hedge) if w_trials else (0.0, [0.0, 0.0], 0)
    err, err_ci, _ = _template_wilson(trials, lambda t: t.error)
    bad_args = sum(1 for t in trials if t.bad_args) / len(trials)
    decided = sum(1 for t in trials if t.decided)
    return {
        "trials": len(trials),
        "templates": n_tpl,
        "accuracy": 1.0 - err,
        "error_rate": err,
        "error_ci95": err_ci,
        "wrong_rate": wrong,
        "wrong_ci95": wrong_ci,
        "hedge_rate": hedge,
        "hedge_ci95": hedge_ci,
        "write_hedge_rate": w_hedge,
        "write_hedge_ci95": w_hedge_ci,
        "bad_args_rate": bad_args,
        "decided": decided,
        "accuracy_decided": sum(1 for t in trials if not t.error) / max(decided, 1),
    }


def aggregate_leaderboard(
    server_records: list[dict[str, Any]],
    all_trials: list[tuple[str, Trial]],
    writes_by_server: dict[str, set[str]],
    model_families: dict[str, str],
    modes: list[str],
    total_spent: float,
) -> dict[str, Any]:
    """Aggregate multi-server results: model table, server x model matrix, cross-family pairs, logit decomposition."""
    flat_trials = [t for _, t in all_trials]
    all_writes = {f"{srv}:{w}" for srv, ws in writes_by_server.items() for w in ws}
    models = sorted({t.model for t in flat_trials})
    families_all = sorted({model_families.get(m, m) for m in models})

    # Tag each trial with server-scoped target for global cross-server logit decomposition
    scoped_trials: list[Trial] = []
    for srv, t in all_trials:
        copy = Trial(**t.to_json())
        copy.target = f"{srv}:{t.target}"
        scoped_trials.append(copy)

    global_logit = decompose_errors(scoped_trials, model_families=model_families, max_tools=250)

    # Per-model summary (leave-own-family-out primary, plus overall and own-family bias)
    model_rows: list[dict[str, Any]] = []
    for m in models:
        mfam = model_families.get(m, "")
        m_trials = [t for t in scoped_trials if t.model == m]
        by_mode = {}
        for mode in modes:
            ts = [t for t in m_trials if t.mode == mode]
            other = [t for t in ts if not t.generator_family or t.generator_family != mfam]
            own = [t for t in ts if t.generator_family and t.generator_family == mfam]
            by_mode[mode] = {
                "other_family": _summarize_subset(other, all_writes),
                "own_family": _summarize_subset(own, all_writes),
                "overall": _summarize_subset(ts, all_writes),
            }
        other_all = [t for t in m_trials if not t.generator_family or t.generator_family != mfam]
        own_all = [t for t in m_trials if t.generator_family and t.generator_family == mfam]
        term = global_logit.get("terms", {}).get(f"model:{m}")
        model_rows.append(
            {
                "model": m,
                "family": mfam,
                "other_family": _summarize_subset(other_all, all_writes),
                "own_family": _summarize_subset(own_all, all_writes),
                "overall": _summarize_subset(m_trials, all_writes),
                "by_mode": by_mode,
                "logit_term": term,
            }
        )
    model_rows.sort(key=lambda r: r["other_family"].get("wrong_rate", 1.0))

    # Server x Model matrix (leave-own-family-out)
    matrix: dict[str, dict[str, dict[str, Any]]] = {}
    for srv_rec in server_records:
        srv = srv_rec["server"]
        ws = writes_by_server.get(srv, set())
        srv_trials = [t for s, t in all_trials if s == srv]
        matrix[srv] = {}
        for m in models:
            mfam = model_families.get(m, "")
            matrix[srv][m] = {}
            for mode in modes:
                ts = [t for t in srv_trials if t.model == m and t.mode == mode]
                other = [t for t in ts if not t.generator_family or t.generator_family != mfam]
                matrix[srv][m][mode] = _summarize_subset(other, ws)

    # Cross-family confused pairs across all servers
    pair_index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for srv, t in all_trials:
        if not (t.wrong and t.chosen) and not t.hedge:
            continue
        mfam = model_families.get(t.model, t.model)
        if t.generator_family and t.generator_family == mfam:
            continue  # leave-own-family-out for pair consensus too
        others = [t.chosen] if (t.wrong and t.chosen) else sorted(set(t.picks) - {t.target})
        for other in others:
            a, b = min(t.target, other), max(t.target, other)
            key = (srv, a, b)
            entry = pair_index.setdefault(
                key,
                {
                    "server": srv,
                    "a": a,
                    "b": b,
                    "write": a in writes_by_server.get(srv, set()) or b in writes_by_server.get(srv, set()),
                    "wrong": 0,
                    "hedge": 0,
                    "directions": Counter(),
                    "families": set(),
                    "models": set(),
                    "modes": set(),
                    "templates": set(),
                    "examples": [],
                },
            )
            if t.wrong:
                entry["wrong"] += 1
                entry["directions"][f"{t.target}>{other}"] += 1
            else:
                entry["hedge"] += 1
            entry["families"].add(mfam)
            entry["models"].add(t.model)
            entry["modes"].add(t.mode)
            entry["templates"].add((t.target, t.request_id))
            if len(entry["examples"]) < 4:
                entry["examples"].append(
                    {
                        "model": t.model,
                        "mode": t.mode,
                        "target": t.target,
                        "called": t.picks,
                        "text": t.text,
                    }
                )

    # Denominator per (server, a, b) over leave-own-family-out trials targeting a or b
    pair_denoms: dict[tuple[str, str], int] = Counter()
    pair_tpls: dict[tuple[str, str], set[str]] = defaultdict(set)
    for srv, t in all_trials:
        mfam = model_families.get(t.model, t.model)
        if t.generator_family and t.generator_family == mfam:
            continue
        pair_denoms[(srv, t.target)] += 1
        pair_tpls[(srv, t.target)].add(t.request_id)

    cross_pairs: list[dict[str, Any]] = []
    for (srv, a, b), e in pair_index.items():
        denom = max(pair_denoms[(srv, a)] + pair_denoms[(srv, b)], 1)
        n_tpl = max(len(pair_tpls[(srv, a)]) + len(pair_tpls[(srv, b)]), 1)
        errs = e["wrong"] + e["hedge"]
        rate = errs / denom
        lo, hi = wilson(rate * n_tpl, n_tpl)
        cross_pairs.append(
            {
                "server": srv,
                "a": a,
                "b": b,
                "write": e["write"],
                "errors": errs,
                "wrong": e["wrong"],
                "hedge": e["hedge"],
                "error_rate": rate,
                "ci95": [lo, hi],
                "templates_confused": len(e["templates"]),
                "families_confused": len(e["families"]),
                "total_families": len(families_all),
                "families": sorted(e["families"]),
                "models": sorted(e["models"]),
                "modes": sorted(e["modes"]),
                "directions": dict(e["directions"]),
                "examples": e["examples"],
            }
        )
    cross_pairs.sort(key=lambda r: (-r["families_confused"], -r["error_rate"], -r["errors"], r["server"], r["a"]))

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "misroute_version": misroute_version(),
        "total_spent_usd": round(total_spent, 4),
        "modes": modes,
        "families": families_all,
        "servers": server_records,
        "models": model_rows,
        "matrix": matrix,
        "cross_family_pairs": cross_pairs[:60],
        "logit": global_logit,
    }


async def run_benchmark(args: argparse.Namespace) -> int:
    load_env(".env")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    req_dir = out_dir / "requests"
    req_dir.mkdir(exist_ok=True)

    server_names = [s.strip() for s in args.servers.split(",") if s.strip()] if args.servers else list(SURFACE_SERVERS)
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]

    if args.mock:
        model_defs, tested, gens, vals = _mock_models()
    else:
        model_defs = [m.strip() for m in args.models.split(",") if m.strip()] if args.models else list(SURFACE_MODELS)
        gens = (
            [g.strip() for g in args.generators.split(",") if g.strip()]
            if args.generators
            else list(SURFACE_GENERATORS)
        )
        vals = (
            [v.strip() for v in args.validators.split(",") if v.strip()]
            if args.validators
            else list(SURFACE_VALIDATORS)
        )
        tested = []

    shared_budget = Budget(args.max_cost)
    server_records: list[dict[str, Any]] = []
    all_trials: list[tuple[str, Trial]] = []
    writes_by_server: dict[str, set[str]] = {}
    model_families: dict[str, str] = {}

    for idx, srv in enumerate(server_names, 1):
        tools_path = SERVERS_DIR / srv / "tools.json"
        meta_path = SERVERS_DIR / srv / "meta.json"
        if not tools_path.is_file():
            print(f"[{idx}/{len(server_names)}] SKIP {srv}: {tools_path} not found", flush=True)
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {"name": srv}
        frozen_path = req_dir / f"{srv}.jsonl"

        cfg_data: dict[str, Any] = {
            "server": {"tools": str(tools_path)},
            "models": model_defs,
            "generators": gens,
            "validators": vals,
            "modes": modes,
            "requests_per_tool": args.requests_per_tool,
            "rotations": args.rotations,
            "repeats": args.repeats,
            "budget_usd": args.max_cost,
        }
        if frozen_path.is_file() and not args.refresh_requests:
            cfg_data["frozen_requests"] = str(frozen_path)
        cfg = Config.model_validate(cfg_data)
        run_models = tested or [m.name for m in cfg.models]
        for m in cfg.models:
            model_families[m.name] = family(m)

        run_root = out_dir / "runs" / srv
        proj = Project(cfg, root=run_root)
        proj.budget = shared_budget

        print(
            f"[{idx}/{len(server_names)}] {srv} ({meta.get('tool_count', '?')} tools) | "
            f"spent=${shared_budget.spent:.2f}/${args.max_cost:.2f} ...",
            flush=True,
        )
        try:
            tools = await proj.tools()
            if not frozen_path.is_file() or args.refresh_requests:
                reqs = await proj.request_set(tools)
                save_requests(frozen_path, reqs)
                proj.cfg.frozen_requests = str(frozen_path)
            res = await proj.discover(models_=run_models, modes=modes)
        except BudgetExceeded as e:
            print(f"  STOPPED (budget cap reached): {e}", flush=True)
            break

        writes_by_server[srv] = set(res.get("writes") or [])
        srv_trials = _load_trials_jsonl(run_root, res["run"])
        for t in srv_trials:
            all_trials.append((srv, t))
        server_records.append(
            {
                "server": srv,
                "display_name": meta.get("display_name", srv),
                "category": meta.get("category", "other"),
                "package": meta.get("package", ""),
                "source_ref": meta.get("source_ref", ""),
                "tool_count": len(tools),
                "write_count": len(writes_by_server[srv]),
                "run": res["run"],
                "validator_agreement": res.get("validator_agreement", {}),
                "manifest": res.get("manifest", {}),
                "spent_usd": round(shared_budget.spent, 4),
            }
        )
        print(
            f"  OK: {len(srv_trials)} trials, cumulative spend=${shared_budget.spent:.2f}",
            flush=True,
        )

    board = aggregate_leaderboard(
        server_records, all_trials, writes_by_server, model_families, modes, shared_budget.spent
    )
    board_path = out_dir / "leaderboard.json"
    board_path.write_text(json.dumps(board, indent=2, default=str) + "\n", encoding="utf-8")

    # Export stratified human-audit sample
    audit_rows = sample_for_audit([t for _, t in all_trials], top_confused=80, random_clean=40, seed=0)
    audit_path = write_audit_jsonl(out_dir / "audit_sample.jsonl", audit_rows)
    print(f"\nSaved leaderboard: {board_path}")
    print(f"Saved audit queue ({len(audit_rows)} templates): {audit_path}")
    print(f"Total spent: ${shared_budget.spent:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a misroute multi-server leaderboard benchmark.")
    parser.add_argument("--preset", default="surface", choices=["surface"])
    parser.add_argument("--servers", help="Comma-separated server slugs from leaderboard/servers/")
    parser.add_argument("--models", help="Comma-separated provider:model specs to evaluate")
    parser.add_argument("--generators", help="Comma-separated generator model specs")
    parser.add_argument("--validators", help="Comma-separated validator model specs")
    parser.add_argument("--modes", default="json,code")
    parser.add_argument("--requests-per-tool", type=int, default=3)
    parser.add_argument("--rotations", type=int, default=2)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--max-cost", type=float, default=7.5)
    parser.add_argument("--out", default="leaderboard/results/surface")
    parser.add_argument("--refresh-requests", action="store_true")
    parser.add_argument("--mock", action="store_true", help="Run offline with deterministic mock models")
    return asyncio.run(run_benchmark(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
