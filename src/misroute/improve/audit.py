import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any
from misroute.lint import descriptions
from misroute.server import openapi
from misroute.server.catalog import Tool

EXCLUDE = re.compile(r"health|ping|metrics|swagger|openapi|debug|internal|admin|login|logout|token|auth", re.I)
VERSION = re.compile(r"/v\d+(?=/|$)", re.I)
VARIANT = re.compile(r"/(search|sok|paginert|query|filter|list|liste|all|alle)$", re.I)


@dataclass
class Note:
    tool: str
    kind: str
    detail: str


def notes(tools: list[Tool], doc: dict | None = None) -> list[Note]:
    """Free findings before any model runs: weak descriptions, cut or clashing names, routes that overlap, and
    operations an agent should probably not see."""
    out = [Note(f.tool, "description", f.issue) for f in descriptions.lint(tools)]
    by_name = defaultdict(list)
    for t in tools:
        by_name[t.name].append(t)
        if t.op_id and len(t.name) == openapi.NAME_LIMIT and len(t.op_id) > openapi.NAME_LIMIT:
            out.append(Note(t.name, "name", f"cut to {openapi.NAME_LIMIT} characters from {t.op_id}"))
        elif t.op_id and t.name != t.op_id:
            out.append(Note(t.name, "name", f"renamed from {t.op_id}"))
        if EXCLUDE.search(f"{t.route} {t.name}"):
            out.append(Note(t.name, "exclude", "looks like plumbing an agent does not need, consider excluding it"))
        if t.route.startswith("POST") and t.read_only:
            out.append(Note(t.name, "hint", "POST that reads, marked read only"))
    for name, same in by_name.items():
        if len(same) > 1:
            routes = ", ".join(t.route or t.name for t in same)
            out.append(Note(name, "name", f"{len(same)} operations share this name: {routes}"))
    groups = defaultdict(list)
    for t in tools:
        if t.route:
            path = t.route.partition(" ")[2]
            groups[(VARIANT.sub("", VERSION.sub("", path.rstrip("/"))), t.is_write)].append(t.name)
    for (key, _), names in groups.items():
        if len(names) > 1:
            for n in names:
                others = ", ".join(x for x in names if x != n)
                out.append(Note(n, "overlap", f"same resource {key} as {others}, consider exposing one"))
    spec = doc or {}
    for fix in spec.get(openapi.FIXES, []):
        out.append(Note("(spec)", "spec", fix))
    ops, hidden = openapi.operations(spec), openapi.secrets(spec)
    for t in tools:
        op = ops.get(t.route)
        if not op:
            continue
        content = openapi.resolve(spec, (op.get("requestBody") or {}).get("content", {}))
        files = openapi.uploads(openapi.merged(openapi.best(content).get("schema") or {})) if content else []
        if files:
            out.append(
                Note(t.name, "upload", f"takes a file in {', '.join(files)}, which an agent cannot pass as text")
            )
        if openapi.binary_only(spec, op):
            out.append(Note(t.name, "binary", "returns a file and no JSON, so an agent gets nothing it can read"))
        for p in openapi.resolve(spec, op.get("parameters") or []):
            if p.get("in") == "header" and p.get("required") and str(p.get("name", "")).lower() not in hidden:
                out.append(
                    Note(
                        t.name,
                        "header",
                        f"required header {p['name']} is a tool argument. If it carries a credential, declare it "
                        "as a security scheme so the server sends it and the agent never sees it",
                    )
                )
    return out


def summary(tools: list[Tool], found: list[Note]) -> dict[str, Any]:
    kinds = defaultdict(int)
    for n in found:
        kinds[n.kind] += 1
    return {
        "tools": len(tools),
        "reads": sum(not t.is_write for t in tools),
        "writes": sum(t.is_write for t in tools),
        "counts": dict(kinds),
        "notes": [asdict(n) for n in found],
    }
