import re
from collections import defaultdict
from dataclasses import replace
from typing import Any
from misroute.requests.generate import fields as output_fields
from misroute.server import merge
from misroute.server.catalog import Tool, words

AUDIENCE_PREFIX = re.compile(r"^(ekstern|external|partner|public|admin|internal|backoffice)$", re.I)
CALLER_SCOPED = re.compile(r"/(me|self|current|my)(?:/|$)|detaljer$|details$|profile$|info$|config$", re.I)
GENERIC_WORDS = frozenset(
    {
        "api",
        "get",
        "list",
        "post",
        "put",
        "patch",
        "delete",
        "create",
        "update",
        "opprett",
        "hent",
        "finn",
        "all",
        "alle",
        "by",
        "id",
        "for",
        "of",
        "the",
        "a",
        "an",
        "v1",
        "v2",
        "v3",
        "view",
        "parent",
        "kind",
        "paginert",
        "filtrert",
        "liste",
        "detaljer",
        "detaljert",
        "status",
        "data",
        "reference",
        "metadata",
    }
)
NAME_MAX = 48


def _stem(word: str) -> str:
    w = word.lower()
    if len(w) > 5 and w.endswith("ene"):
        return w[:-2]
    if len(w) > 4 and w.endswith(("er", "es")):
        return w[:-1]
    if len(w) > 4 and w.endswith(("r", "s")):
        return w[:-1]
    return w


def _domain_stems(tool: Tool) -> set[str]:
    source = tool.op_id or tool.name
    ws = words(source)
    return {_stem(w) for w in ws if len(w) > 2 and w not in GENERIC_WORDS and not AUDIENCE_PREFIX.match(w)}


def parallel_audiences(tools: list[Tool]) -> dict[str, str]:
    """Detect secondary audience surfaces (e.g. Ekstern, Admin, Internal) that duplicate primary domain nouns.

    Mixing two audiences in one MCP server creates direct sibling collisions (such as internal vs. external
    create/status endpoints). Returns {tool_name: exclusion_reason}.
    """
    secondary: list[tuple[str, Tool]] = []
    primary_stems: set[str] = set()
    for t in tools:
        path_parts = [
            p for p in (t.route.partition(" ")[2] if t.route else "").split("/") if p and not p.startswith("{")
        ]
        name_head = (t.op_id or t.name).split("_")[0]
        aud = next((p for p in [name_head, *path_parts] if AUDIENCE_PREFIX.match(p)), None)
        if aud:
            secondary.append((aud.lower(), t))
        else:
            primary_stems |= _domain_stems(t)
    out: dict[str, str] = {}
    for aud, t in secondary:
        if _domain_stems(t) & primary_stems:
            out[t.name] = (
                f"parallel {aud} surface duplicating primary domain operations; "
                "expose in a separate MCP server for that audience"
            )
    return out


def is_caller_or_singleton(tool: Tool) -> bool:
    """True when a zero-arg GET is caller-scoped (/me) or returns a single object rather than a lookup list."""
    path = tool.route.partition(" ")[2] if tool.route else ""
    if CALLER_SCOPED.search(path) or CALLER_SCOPED.search(tool.op_id or tool.name):
        return True
    schema = tool.output_schema or {}
    if schema.get("type") == "object" and "items" not in schema:
        props = schema.get("properties") or {}
        has_list = any(isinstance(v, dict) and (v.get("type") == "array" or "items" in v) for v in props.values())
        return not has_list
    return False


def _trim_words(ws: list[str], limit: int = NAME_MAX) -> str:
    joined = "_".join(ws)
    if len(joined) <= limit:
        return joined
    kept: list[str] = []
    for w in ws:
        cand = "_".join([*kept, w])
        if len(cand) <= limit:
            kept.append(w)
        else:
            break
    return "_".join(kept) if kept else joined[:limit].rstrip("_")


def clean_name(tool: Tool, taken: set[str]) -> str:
    """E2 name normalization: short, intent-based snake_case without redundant controller prefixes or truncation."""
    raw = tool.op_id or tool.name
    if "_" in raw and not tool.operations:
        ctrl, _, action = raw.partition("_")
        ctrl_ws = words(ctrl)
        act_ws = words(action)
        ctrl_stems = {_stem(w) for w in ctrl_ws}
        act_stems = {_stem(w) for w in act_ws}
        # Drop controller prefix when the action already names the resource noun
        if act_ws and (ctrl_stems & act_stems):
            ws = act_ws
        else:
            ws = [*act_ws[:1], *ctrl_ws, *act_ws[1:]] if act_ws else ctrl_ws
    else:
        ws = words(raw)
    # Remove consecutive duplicate words and normalize common Norwegian/English duplicates
    dedup: list[str] = []
    for w in ws:
        if not dedup or _stem(dedup[-1]) != _stem(w):
            dedup.append(w)
    if dedup and dedup[0] == "opprett":
        dedup[0] = "create"
    if dedup and dedup[0] == "hent":
        dedup[0] = "get"
    base = _trim_words(dedup or ["tool"], NAME_MAX)
    name = base
    n = 2
    while name in taken:
        suffix = f"_{n}"
        name = f"{base[: NAME_MAX - len(suffix)].rstrip('_')}{suffix}"
        n += 1
    taken.add(name)
    return name


def _short_purpose(tool: Tool) -> str:
    """Concise clause describing what this tool does, used inside E1 boundary statements."""
    if tool.operations:
        vals = ", ".join(list(tool.operations)[:4])
        more = ", ..." if len(tool.operations) > 4 else ""
        return f"selecting {tool.selector} ({vals}{more})"
    desc = " ".join((tool.description or "").split())
    if desc and not desc.startswith("Executes "):
        first = re.split(r"(?<=[.!?])\s", desc)[0].rstrip(".")
        return first[0].lower() + first[1:] if len(first) <= 90 else first[:87].rstrip() + "..."
    verb = "creating/updating" if tool.is_write else "fetching"
    ws = [w for w in words(tool.name) if w not in ("get", "list", "create", "update")]
    noun = " ".join(ws) or tool.name
    if tool.required:
        return f"{verb} {noun} by {', '.join(tool.required[:2])}"
    return f"{verb} {noun}"


def _base_description(tool: Tool) -> str:
    """Schema-grounded description replacing 'Executes GET /api/...' stubs with zero hallucination."""
    desc = " ".join((tool.description or "").split())
    if desc and not desc.startswith("Executes "):
        return desc
    if tool.operations:
        vals = ", ".join(tool.operations)
        return f"Read-only consolidated tool with selector `{tool.selector}` ({vals})."
    ws = words(tool.name)
    verb = ws[0] if ws else ("create" if tool.is_write else "get")
    noun = " ".join(ws[1:]) or tool.name
    action_map = {
        "get": "Gets",
        "list": "Lists",
        "create": "Creates",
        "update": "Updates",
        "delete": "Deletes",
        "paginert": "Lists paginated",
    }
    lead = action_map.get(verb, "Updates" if tool.is_write else "Retrieves")
    parts = [f"{lead} {noun}."]
    if tool.required:
        parts.append(f"Requires: {', '.join(tool.required[:5])}.")
    constraints = []
    for k, schema in tool.params.items():
        if not isinstance(schema, dict):
            continue
        if schema.get("enum"):
            allowed = ", ".join(str(v) for v in schema["enum"][:6])
            constraints.append(f"`{k}` in [{allowed}]")
        elif schema.get("minimum") is not None or schema.get("maximum") is not None:
            lo = schema.get("minimum", "")
            hi = schema.get("maximum", "")
            constraints.append(f"`{k}` range [{lo}..{hi}]")
    if constraints:
        parts.append("Constraints: " + "; ".join(constraints[:4]) + ".")
    ret = output_fields(tool.output_schema)
    if ret:
        parts.append(f"Returns fields: {', '.join(ret[:6])}.")
    return " ".join(parts)


def static_siblings(tools: list[Tool]) -> dict[str, list[Tool]]:
    """Identify likely sibling pairs statically from shared domain stems (same resource noun)."""
    stems = {t.name: _domain_stems(t) for t in tools}
    sibs: dict[str, list[tuple[int, Tool]]] = defaultdict(list)
    for i, a in enumerate(tools):
        for b in tools[i + 1 :]:
            shared = stems[a.name] & stems[b.name]
            if shared:
                score = len(shared) * 2 + (1 if a.is_write == b.is_write else 0)
                sibs[a.name].append((score, b))
                sibs[b.name].append((score, a))
    out: dict[str, list[Tool]] = {}
    for name, rows in sibs.items():
        rows.sort(key=lambda x: (-x[0], x[1].name))
        out[name] = [t for _, t in rows[:2]]
    return out


def apply_e1_e2(tools: list[Tool]) -> tuple[list[Tool], dict[str, str]]:
    """Apply E2 (clean sibling-distinct naming) and E1 (explicit sibling boundary clauses) to a tool catalog.

    Returns (polished_tools, old_to_new_name_map). Descriptions are static drafts until confirmed with
    misroute check / improve against a sham neutral baseline.
    """
    taken: set[str] = set()
    rename_map: dict[str, str] = {}
    renamed: list[Tool] = []
    for t in tools:
        new_name = clean_name(t, taken)
        rename_map[t.name] = new_name
        renamed.append(replace(t, name=new_name))

    # First build schema-grounded base descriptions so boundary clauses can quote clear purposes
    grounded = [replace(t, description=_base_description(t)) for t in renamed]
    sib_map = static_siblings(grounded)
    final: list[Tool] = []
    for t in grounded:
        sibs = sib_map.get(t.name) or []
        if sibs:
            clauses = [f"Not for {_short_purpose(s)} (use `{s.name}` instead)." for s in sibs]
            desc = f"{t.description} {' '.join(clauses)}"
        else:
            desc = t.description
        final.append(replace(t, description=desc))
    return final, rename_map


def propose_clean(tools: list[Tool], doc: dict | None = None, split_audiences: bool = True) -> dict[str, Any]:
    """Propose a clean catalog plan: split parallel audiences and keep caller/singleton GETs out of reference_data."""
    base = merge.propose(tools, doc)
    exclude = dict(base.get("exclude") or {})
    if split_audiences:
        for name, why in parallel_audiences(tools).items():
            exclude.setdefault(name, why)

    # Tighten reference_data: drop caller-scoped (/me) or singleton-detail routes and any newly excluded ops
    by_name = {t.name: t for t in tools}
    cleaned_tools: dict[str, Any] = {}
    for mname, body in (base.get("tools") or {}).items():
        ops = {}
        for sel, spec in (body.get("operations") or {}).items():
            op_name = spec["operation"] if isinstance(spec, dict) else str(spec)
            if op_name in exclude:
                continue
            if mname == "reference_data" and op_name in by_name and is_caller_or_singleton(by_name[op_name]):
                continue
            ops[sel] = spec
        if len(ops) >= 2:
            cleaned_tools[mname] = {**body, "operations": ops}

    return {"note": base.get("note", merge.NOTE), "tools": cleaned_tools, "exclude": exclude}
