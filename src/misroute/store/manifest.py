import contextlib
import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from misroute.config import Config, family
from misroute.detectors.rank import wilson
from misroute.server.catalog import Tool
from misroute.trial import Request, Trial

AUDIT_BAD_LABELS = frozenset({"ambiguous_request", "wrong_ground_truth", "infeasible_for_schema"})
AUDIT_VALID_LABELS = frozenset({"valid_misroute", "valid_clean"})


def misroute_version() -> str:
    try:
        return version("misroute")
    except PackageNotFoundError:
        return "dev"


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_manifest(
    cfg: Config,
    tools: list[Tool],
    catalog_digest: str,
    recipe_digest: str,
    run_id: str,
    phase: str,
) -> dict[str, Any]:
    """Reproducibility manifest freezing versions, pinned model IDs, options, server snapshot hashes and recipe."""
    server_info: dict[str, Any] = {
        "label": cfg.server.label,
        "url": cfg.server.url,
        "command": cfg.server.command,
        "openapi": cfg.server.openapi,
        "tools": cfg.server.tools,
        "catalog": cfg.server.catalog,
    }
    for key in ("tools", "openapi"):
        val = getattr(cfg.server, key)
        if val and Path(val).is_file():
            p = Path(val)
            server_info[f"{key}_sha256"] = file_sha256(p)
            meta_path = p.parent / "meta.json"
            if meta_path.is_file():
                with contextlib.suppress(ValueError):
                    server_info["meta"] = json.loads(meta_path.read_text(encoding="utf-8"))
    if cfg.frozen_requests and Path(cfg.frozen_requests).is_file():
        server_info["frozen_requests"] = cfg.frozen_requests
        server_info["frozen_requests_sha256"] = file_sha256(cfg.frozen_requests)

    models_info = [
        {
            "name": m.name,
            "provider": m.provider,
            "model_id": m.model_id,
            "family": family(m),
            "options": dict(m.options),
        }
        for m in cfg.models
    ]
    return {
        "misroute_version": misroute_version(),
        "date_utc": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "phase": phase,
        "catalog_hash": catalog_digest,
        "request_recipe": recipe_digest,
        "server": server_info,
        "tool_count": len(tools),
        "write_count": sum(1 for t in tools if t.is_write),
        "has_output_schema_count": sum(1 for t in tools if t.output_schema),
        "models": models_info,
        "tested": cfg.tested(),
        "generators": [m.name for m in cfg.generator_models()],
        "validators": [m.name for m in cfg.validator_models()],
        "modes": list(cfg.modes),
        "request_source": cfg.request_source,
        "request_languages": list(cfg.request_languages),
        "requests_per_tool": cfg.requests_per_tool,
        "slots": cfg.slots,
        "rotations": cfg.rotations,
        "repeats": cfg.repeats,
        "max_turns": cfg.max_turns,
    }


def save_requests(path: str | Path, reqs: dict[str, list[Request]]) -> str:
    """Freeze a validated request corpus to JSONL and return its SHA-256 digest."""
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for tool_name in sorted(reqs):
        for r in reqs[tool_name]:
            lines.append(json.dumps(asdict(r), sort_keys=True))
    content = ("\n".join(lines) + "\n") if lines else ""
    out_path.write_text(content, encoding="utf-8")
    return file_sha256(out_path)


def load_requests(path: str | Path, allowed_tools: set[str] | None = None) -> dict[str, list[Request]]:
    """Load a frozen request corpus from JSONL, optionally filtered to allowed_tools."""
    out: dict[str, list[Request]] = defaultdict(list)
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        req = Request(**row)
        if allowed_tools is None or req.tool in allowed_tools:
            out[req.tool].append(req)
    return dict(out)


def sample_for_audit(
    trials: list[Trial],
    top_confused: int = 100,
    random_clean: int = 50,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """Build a stratified human-audit sample: top confused templates plus a random sample of clean templates."""
    by_tpl: dict[tuple[str, str], list[Trial]] = defaultdict(list)
    for t in trials:
        by_tpl[(t.target, t.request_id)].append(t)

    confused_rows: list[dict[str, Any]] = []
    clean_rows: list[dict[str, Any]] = []
    for (target, req_id), ts in sorted(by_tpl.items()):
        n = len(ts)
        errs = sum(1 for t in ts if t.error)
        wrongs = sum(1 for t in ts if t.wrong)
        hedges = sum(1 for t in ts if t.hedge)
        picks = Counter(t.chosen for t in ts if t.wrong and t.chosen)
        models_erred = sorted({t.model for t in ts if t.error})
        row = {
            "target": target,
            "request_id": req_id,
            "text": ts[0].text,
            "kind": ts[0].kind,
            "lang": ts[0].lang,
            "generator_model": ts[0].generator_model,
            "generator_family": ts[0].generator_family,
            "trials": n,
            "error_rate": errs / n,
            "wrong_rate": wrongs / n,
            "hedge_rate": hedges / n,
            "top_wrong_pick": picks.most_common(1)[0][0] if picks else None,
            "models_erred": models_erred,
            "label": "",
            "notes": "",
        }
        if errs > 0:
            row["stratum"] = "confused"
            confused_rows.append(row)
        else:
            row["stratum"] = "clean"
            clean_rows.append(row)

    confused_rows.sort(key=lambda r: (-r["error_rate"], -len(r["models_erred"]), r["target"], r["request_id"]))
    picked_confused = confused_rows[:top_confused]
    rng = random.Random(seed)
    picked_clean = rng.sample(clean_rows, min(random_clean, len(clean_rows)))
    return picked_confused + picked_clean


def write_audit_jsonl(path: str | Path, rows: list[dict[str, Any]]) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(json.dumps(r) for r in rows) + ("\n" if rows else ""), encoding="utf-8")
    return out_path


def score_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize human-audit labels with Wilson 95% CIs for audited precision and label error rate."""
    labeled = [r for r in rows if str(r.get("label", "")).strip()]
    counts = Counter(str(r["label"]).strip() for r in labeled)
    confused = [r for r in labeled if r.get("stratum") == "confused"]
    valid_misroutes = sum(1 for r in confused if str(r["label"]).strip() == "valid_misroute")
    bad_total = sum(1 for r in labeled if str(r["label"]).strip() in AUDIT_BAD_LABELS)

    n_all = len(labeled)
    n_conf = len(confused)
    return {
        "total_sampled": len(rows),
        "labeled": n_all,
        "counts": dict(counts),
        "confused_labeled": n_conf,
        "audited_precision": valid_misroutes / n_conf if n_conf else None,
        "audited_precision_ci95": list(wilson(valid_misroutes, n_conf)) if n_conf else None,
        "label_error_rate": bad_total / n_all if n_all else None,
        "label_error_rate_ci95": list(wilson(bad_total, n_all)) if n_all else None,
    }
