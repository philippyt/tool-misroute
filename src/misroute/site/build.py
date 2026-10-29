import re
import time
from pathlib import Path
from jinja2 import Environment, PackageLoader, select_autoescape
from misroute.report.html import html
from misroute.store.runs import Store


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "run"


def entry(name: str, root: Path) -> dict:
    store = Store(root)
    disc = store.read("discover") or {}
    fix = store.read("fix") or {}
    lint = store.read("lint") or {}
    return {
        "name": name,
        "slug": slug(name),
        "store": store,
        "tools": lint.get("tools"),
        "weak": len(lint.get("findings", [])),
        "cells": disc.get("cells", []),
        "spots": disc.get("spots", [])[:5],
        "gate_failed": len(disc.get("gate_failed", [])),
        "fix": fix.get("across", []),
    }


def build(results: list[tuple[str, Path]], out: Path, title: str = "misroute results") -> Path:
    out.mkdir(parents=True, exist_ok=True)
    entries = [entry(name, root) for name, root in results]
    for e in entries:
        (out / f"{e['slug']}.html").write_text(html(e["store"]), encoding="utf-8")
    env = Environment(loader=PackageLoader("misroute.site"), autoescape=select_autoescape())
    env.filters["pct"] = lambda v: f"{100 * (v or 0):.1f}%"
    page = env.get_template("index.html.j2").render(title=title, entries=entries, built=time.strftime("%Y %m %d"))
    path = out / "index.html"
    path.write_text(page, encoding="utf-8")
    return path
