from pathlib import Path
from typing import Any
from jinja2 import Environment, PackageLoader, select_autoescape
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
        cells.append([(c, matrix[r].get(c, 0), matrix[r].get(c, 0) / total, c == r) for c in cols])
    return {"rows": rows, "cols": cols, "cells": cells}


def render(store: Store, out: str | Path) -> Path:
    env = Environment(loader=PackageLoader("misroute.report"), autoescape=select_autoescape())
    env.filters["heat"] = heat
    env.filters["pct"] = lambda v: f"{100 * (v or 0):.1f}%"
    env.filters["pval"] = lambda v: "" if v is None else (f"{v:.3f}" if v >= 0.001 else "<0.001")
    discover = store.read("discover")
    if discover:
        for c in discover["cells"]:
            c["heat"] = heatmap(c["matrix"])
    html = env.get_template("report.html.j2").render(
        lint=store.read("lint"), discover=discover, probe=store.read("probe"), fix=store.read("fix")
    )
    path = Path(out)
    path.write_text(html, encoding="utf-8")
    return path
