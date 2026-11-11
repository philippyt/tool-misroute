import re
from collections import defaultdict
from pathlib import Path
from typing import Any
import yaml
from misroute.server import openapi
from misroute.server.catalog import Tool

NOTE = (
    "Proposed by misroute from routes and parameters alone. Review before use: rename tools and values, write "
    "descriptions, remove merges that do not share one intent. A merge keeps every operation reachable through "
    "its selector value. Only exclude drops an operation, and each exclusion carries its reason."
)
SIMILAR = 0.5
REFERENCE_MIN = 3


def load_plan(path: str | Path) -> dict[str, Any]:
    """A catalog plan: tools that each merge several operations behind a selector, and operations to exclude."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"catalog plan {path} must be a mapping with tools and exclude")
    return {"tools": data.get("tools") or {}, "exclude": data.get("exclude") or {}}


def members(body: dict) -> dict[str, tuple[str, dict[str, str]]]:
    """Selector value to the operation it runs and its argument renames, merged name to the operation's own."""
    out = {}
    for value, spec in (body.get("operations") or {}).items():
        if isinstance(spec, dict):
            out[str(value)] = (str(spec.get("operation", "")), dict(spec.get("args") or {}))
        else:
            out[str(value)] = (str(spec), {})
    return out


def lookup(tools: list[Tool]) -> dict[str, Tool]:
    return {k: t for t in tools for k in (t.name, t.op_id, t.route) if k}


def check(tools: list[Tool], plan: dict) -> list[str]:
    """Reasons a plan cannot be applied. Every operation must end up reachable through exactly one tool or be
    excluded on purpose, and a merged tool must not mix reads with writes."""
    by, out = lookup(tools), []
    gone = set()
    for key in plan.get("exclude") or {}:
        if key not in by:
            out.append(f"exclude names {key}, which is not an operation")
        else:
            gone.add(by[key].name)
    used: dict[str, str] = {}
    names = {t.name for t in tools}
    for name, body in (plan.get("tools") or {}).items():
        parts = members(body)
        selector = body.get("selector") or "operation"
        if len(parts) < 2:
            out.append(f"{name} merges fewer than two operations")
        found = []
        for value, (op, renames) in parts.items():
            if op not in by:
                out.append(f"{name}.{value} names {op}, which is not an operation")
                continue
            tool = by[op]
            if tool.name in gone:
                out.append(f"{name}.{value} uses {tool.name}, which is also excluded")
            if tool.name in used:
                out.append(f"{tool.name} is in both {used[tool.name]} and {name}")
            used[tool.name] = name
            found.append(tool)
            for new, old in renames.items():
                if old not in tool.params:
                    out.append(f"{name}.{value} renames {old}, which {tool.name} does not take")
                if new == selector:
                    out.append(f"{name}.{value} renames an argument to the selector name {selector}")
            if selector in set(tool.params) - set(renames.values()):
                out.append(f"{name}: selector {selector} is also an argument of {tool.name}, pick another name")
        if len({t.is_write for t in found}) > 1:
            out.append(f"{name} mixes reads and writes, which hides a write behind a read only tool")
        if name in names - {t.name for t in found}:
            out.append(f"{name} is already the name of another tool")
    return out


def said(tool: Tool) -> str:
    """What an operation does in a few words: its description, or its route when it has none of its own."""
    text = " ".join((tool.description or "").split())
    if not text or text.startswith("Executes "):
        return tool.route or tool.name
    first = re.split(r"(?<=[.!?])\s", text)[0]
    return first if len(first) <= 140 else first[:137] + "..."


def combine(name: str, body: dict, parts: list[tuple[str, Tool, dict[str, str]]]) -> Tool:
    """One tool for several operations. It takes the selector, required, and every argument of every operation.
    An argument is required only when all of them require it, and one that only some take says which."""
    selector = body.get("selector") or "operation"
    props: dict[str, Any] = {}
    takes: dict[str, list[str]] = defaultdict(list)
    needs: dict[str, list[str]] = defaultdict(list)
    for value, tool, renames in parts:
        back = {old: new for new, old in renames.items()}
        for pname, schema in tool.params.items():
            key = back.get(pname, pname)
            props.setdefault(key, dict(schema) if isinstance(schema, dict) else {})
            takes[key].append(value)
            if pname in tool.required:
                needs[key].append(value)
    values = [v for v, _, _ in parts]
    for key, used in takes.items():
        notes = []
        if len(used) < len(values):
            notes.append(f"Only with {selector} {', '.join(used)}.")
        if needs[key] and len(needs[key]) < len(values):
            notes.append(f"Required with {selector} {', '.join(needs[key])}.")
        if notes:
            props[key]["description"] = " ".join([props[key].get("description") or "", *notes]).strip()
    choose = "; ".join(f"{v}: {said(t)}" for v, t, _ in parts)
    picker = {"type": "string", "enum": values, "description": f"Which one to run. {choose}"}
    required = [selector, *[k for k, used in needs.items() if len(used) == len(values)]]
    schema = {"type": "object", "properties": {selector: picker, **props}, "required": required}
    outs = [t.output_schema for _, t, _ in parts]
    title = body.get("title") or name.replace("_", " ")
    description = body.get("description") or f"{title}. Set {selector} to choose: {choose}"
    return Tool(
        name=name,
        description=description,
        input_schema=schema,
        output_schema=outs[0] if all(o == outs[0] for o in outs) else None,
        read_only=not parts[0][1].is_write,
        destructive=any(t.destructive for _, t, _ in parts),
        selector=selector,
        operations={v: t.name for v, t, _ in parts},
    )


def apply(tools: list[Tool], plan: dict) -> list[Tool]:
    """The catalog the plan describes: merged tools where their first operation stood, excluded operations
    gone, everything else as it was."""
    problems = check(tools, plan)
    if problems:
        raise ValueError("catalog plan: " + "; ".join(problems))
    by = lookup(tools)
    gone = {by[k].name for k in plan.get("exclude") or {}}
    made: dict[str, Tool] = {}
    taken: set[str] = set()
    for name, body in (plan.get("tools") or {}).items():
        parts = [(value, by[op], renames) for value, (op, renames) in members(body).items()]
        made[parts[0][1].name] = combine(name, body, parts)
        taken |= {t.name for _, t, _ in parts}
    out = []
    for t in tools:
        if t.name in made:
            out.append(made[t.name])
        elif t.name not in gone and t.name not in taken:
            out.append(t)
    return out


def reach(tools: list[Tool]) -> dict[str, tuple[str, str]]:
    """Each original operation name with the tool that reaches it now and the selector value that picks it."""
    out = {}
    for t in tools:
        if t.operations:
            out.update({name: (t.name, value) for value, name in t.operations.items()})
        else:
            out[t.name] = (t.name, "")
    return out


def coverage(source: list[Tool], plan: dict) -> dict[str, Any]:
    """Where every original operation went: reached through which tool and value, or excluded and why."""
    final = apply(source, plan)
    where = reach(final)
    by = lookup(source)
    excluded = {by[k].name: str(v) for k, v in (plan.get("exclude") or {}).items()}
    lost = [t.name for t in source if t.name not in where and t.name not in excluded]
    return {
        "operations": len(source),
        "tools": len(final),
        "merged": {t.name: dict(t.operations) for t in final if t.operations},
        "excluded": excluded,
        "unreachable": lost,
    }


def route_path(tool: Tool) -> str:
    return tool.route.partition(" ")[2].rstrip("/")


def alike(a: Tool, b: Tool) -> bool:
    x, y = set(a.params), set(b.params)
    return (not x and not y) or len(x & y) / len(x | y) >= SIMILAR


def unique(name: str, taken: set[str]) -> str:
    out, n = name, 2
    while out in taken:
        out, n = f"{name}_{n}", n + 1
    taken.add(out)
    return out


def propose(tools: list[Tool], doc: dict | None = None) -> dict[str, Any]:
    """A first plan from routes and parameters, with no model call, for a person to review. Three shapes that
    generated APIs repeat and that share one intent: the same listing under different parents, variants of one
    collection that take the same filters, and lists with no parameters. Only reads are merged. Operations an
    agent cannot use, such as file uploads and reads whose reply is only a file, are proposed for exclusion. A
    write stays even when its reply is a file, since its effect is the point."""
    from misroute.improve import audit

    writes = {t.name for t in tools if t.is_write}
    exclude = {
        n.tool: n.detail
        for n in audit.notes(tools, doc)
        if n.kind in ("upload", "exclude") or (n.kind == "binary" and n.tool not in writes)
    }
    reads = [t for t in tools if t.route and t.name not in exclude and not t.is_write]
    taken = {t.name for t in tools}
    paths = {route_path(t) for t in tools if t.route}
    used: set[str] = set()
    out: dict[str, Any] = {}

    under: dict[tuple[str, str], list[tuple[str, Tool]]] = defaultdict(list)
    for t in reads:
        m = re.fullmatch(r"(.*)/([^/{}]+)/\{([^/{}]+)\}/([^/{}]+)", route_path(t))
        if m and t.route.startswith("GET ") and len(t.required) == 1:
            under[(m[1], m[4])].append((m[2], t))
    for (_, leaf), found in under.items():
        if len(found) < 2 or len({frozenset(set(t.params) - set(t.required)) for _, t in found}) > 1:
            continue
        ops = {parent.lower(): {"operation": t.name, "args": {"id": t.required[0]}} for parent, t in found}
        out[unique(f"{leaf}_by_parent", taken)] = {
            "selector": "parent",
            "why": f"the same {leaf} listing under {len(found)} different parents",
            "operations": ops,
        }
        used |= {t.name for _, t in found}

    variants: dict[str, list[tuple[str, Tool]]] = defaultdict(list)
    for t in reads:
        path = route_path(t)
        parent, _, last = path.rpartition("/")
        if t.name in used or "{" in last:
            continue
        if parent in paths:
            variants[parent].append((last.lower(), t))
        if path in paths and t.route.startswith("GET "):
            variants[path].append(("list", t))
    for parent, found in variants.items():
        group = [(v, t) for v, t in found if t.name not in used and alike(found[0][1], t)]
        if len(group) < 2 or len({v for v, _ in group}) < len(group):
            continue
        resource = parent.rpartition("/")[2]
        out[unique(f"{resource}_view", taken)] = {
            "selector": "view",
            "why": f"{len(group)} forms of the same {resource} query that take the same arguments",
            "operations": {v: t.name for v, t in group},
        }
        used |= {t.name for _, t in group}

    bare = [t for t in reads if t.name not in used and not t.params and t.route.startswith("GET ")]
    if len(bare) >= REFERENCE_MIN:
        common = 0
        split = [route_path(t).strip("/").split("/") for t in bare]
        while all(len(s) > common + 1 for s in split) and len({s[common] for s in split}) == 1:
            common += 1
        kinds = ["_".join(s[common:]).lower() for s in split]
        if len(set(kinds)) == len(kinds):
            out[unique("reference_data", taken)] = {
                "selector": "kind",
                "why": f"{len(bare)} lists that take no arguments",
                "operations": dict(zip(kinds, [t.name for t in bare], strict=True)),
            }
    return {"note": NOTE, "tools": out, "exclude": exclude}


def calls(source: list[Tool], final: list[Tool], plan: dict, doc: dict) -> dict[str, Any]:
    """For each merged tool, what a server needs to run it: its schema and hints, and per selector value the
    method, the path, where each argument goes and how the body is built."""
    by = lookup(source)
    ops = {
        f"{m.upper()} {route}": (item.get("parameters", []), item[m])
        for route, item in (doc.get("paths") or {}).items()
        for m in openapi.METHODS
        if item.get(m)
    }
    out = {}
    for t in final:
        if not t.operations:
            continue
        body = (plan.get("tools") or {})[t.name]
        table = {}
        for value, (op, renames) in members(body).items():
            member = by[op]
            back = {old: new for new, old in renames.items()}
            shared, spec = ops[member.route]
            params, fields, whole = openapi.shape(doc, shared, spec)
            method, _, path = member.route.partition(" ")
            table[value] = {
                "method": method,
                "path": path,
                "params": [{"name": p["name"], "in": p.get("in", "query"), "arg": back.get(k, k)} for k, p in params],
                "fields": [{"name": f, "arg": back.get(f, f)} for f in fields or {}],
                "body": "fields" if fields else "whole" if whole else None,
            }
        out[t.name] = {
            "description": t.description,
            "parameters": t.input_schema,
            "read_only": not t.is_write,
            "destructive": bool(t.destructive),
            "selector": t.selector,
            "operations": table,
        }
    return out
