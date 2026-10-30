import json
from collections import Counter
from pathlib import Path
from typing import Any
from jinja2 import Environment, PackageLoader, select_autoescape
from misroute.detectors.confusion import READ_FIRST
from misroute.store.runs import Store


def heat(value: float) -> str:
    v = max(0.0, min(1.0, value))
    return f"rgba(214, 69, 65, {0.08 + 0.8 * v:.2f})" if v > 0 else "transparent"


def heatmap(matrix: dict[str, dict[str, int]]) -> dict[str, Any]:
    rows = sorted(matrix)
    cols = sorted({c for v in matrix.values() for c in v} | set(rows))
    cells = []
    for r in rows:
        total = sum(matrix[r].values()) or 1
        cells.append(
            [(c, matrix[r].get(c, 0), 0 if c == READ_FIRST else matrix[r].get(c, 0) / total, c == r) for c in cols]
        )
    return {"rows": rows, "cols": cols, "cells": cells}


def guard_summary(log: str | Path | None) -> dict[str, Any] | None:
    """Real traffic seen by the guard: events per kind and per tool pair, and held writes."""
    path = Path(log) if log else None
    if not path or not path.exists():
        return None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    events = [r for r in rows if r.get("type") == "event"]
    pairs = Counter(
        (r.get("kind"), *sorted((r.get("tool") or "", r.get("other") or ""))) for r in events if r.get("other")
    )
    return {
        "calls": sum(r.get("type") == "call" for r in rows),
        "held": sum(r.get("type") == "held" for r in rows),
        "kinds": dict(Counter(r.get("kind") for r in events)),
        "pairs": [{"kind": k, "a": a, "b": b, "count": c} for (k, a, b), c in pairs.most_common(15)],
    }


def render(store: Store, out: str | Path, guard_log: str | Path | None = None) -> Path:
    path = Path(out)
    path.write_text(html(store, guard_log), encoding="utf-8")
    return path


def html(store: Store, guard_log: str | Path | None = None) -> str:
    env = Environment(loader=PackageLoader("misroute.report"), autoescape=select_autoescape())
    env.filters["heat"] = heat
    env.filters["pct"] = lambda v: f"{100 * (v or 0):.1f}%"
    env.filters["pval"] = lambda v: "" if v is None else (f"{v:.3f}" if v >= 0.001 else "<0.001")
    discover = store.read("discover")
    if discover:
        for c in discover["cells"]:
            c["heat"] = heatmap(c["matrix"])
    return env.get_template("report.html.j2").render(
        lint=store.read("lint"),
        discover=discover,
        probe=store.read("probe"),
        fix=store.read("fix"),
        stale=store.stale(),
        guard=guard_summary(guard_log),
    )
