import re
from collections import defaultdict
from dataclasses import replace
from typing import Any
from misroute.requests.generate import fields as output_fields
from misroute.server import merge, openapi
from misroute.server.catalog import Tool, words

AUDIENCE_PREFIX = re.compile(r"^(ekstern|external|partner|public|admin|internal|backoffice)$", re.I)
CALLER_SCOPED = re.compile(r"/(me|self|current|my)(?:/|$)|detaljer$|details$|profile$|info$|config$", re.I)
SUSPICIOUS_BINARY = re.compile(r"schema|feature|geojson|json|export|report", re.I)
FILLER_WORDS = frozenset({"detaljert", "paginert", "filtrert", "filtered", "paginated", "detailed", "liste", "list"})
TRAILING_PREPS = frozenset({"med", "for", "i", "av", "til", "by", "of", "with", "to", "in", "on", "from"})
PAGING_FIELDS = frozenset(
    {
        "sidenummer",
        "sidestorrelse",
        "antallsider",
        "totaltantallelementer",
        "page",
        "pagesize",
        "totalcount",
        "totalpages",
        "nextcursor",
        "continuationtoken",
        "cursor",
        "hasmore",
    }
)
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
        "ider",
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
        "med",
        "my",
        "me",
        "user",
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
    """Domain noun stems from the tool name AND any merged operations inside tool.operations."""
    parts = [tool.op_id or tool.name, tool.name]
    if tool.operations:
        parts.extend(tool.operations.keys())
        parts.extend(tool.operations.values())
    stems: set[str] = set()
    for part in parts:
        for w in words(part):
            if len(w) > 2 and w not in GENERIC_WORDS and not AUDIENCE_PREFIX.match(w) and not w.isdigit():
                stems.add(_stem(w))
    return stems


def parallel_audiences(tools: list[Tool]) -> dict[str, str]:
    """Detect secondary audience surfaces (e.g. Ekstern, Admin, Internal) duplicating primary domain nouns."""
    secondary: list[tuple[str, Tool]] = []
    primary_stems: set[str] = set()
    for t in tools:
        raw_path = t.route.partition(" ")[2] if t.route else ""
        path_parts = [p for p in raw_path.split("/") if p and not p.startswith("{")]
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


def _from_route(tool: Tool) -> list[str]:
    """Fallback word sequence derived from HTTP method + route path when operationId is truncated/noisy."""
    method, _, path = tool.route.partition(" ")
    segs = [p for p in path.split("/") if p and p.lower() != "api" and not re.fullmatch(r"v\d+", p, re.I)]
    ws: list[str] = []
    if method == "POST":
        ws.append("list" if tool.read_only else "create")
    elif method == "PUT" or method == "PATCH":
        ws.append("update")
    elif method == "DELETE":
        ws.append("delete")
    else:
        has_id = any(s.startswith("{") for s in segs)
        ws.append("get" if (has_id and segs and segs[-1].startswith("{")) else "list")
    for s in segs:
        if s.startswith("{"):
            continue
        ws.extend(words(s))
    return ws


def _trim_words(ws: list[str], limit: int = NAME_MAX) -> str:
    # Drop filler words first if over limit
    if len("_".join(ws)) > limit:
        pruned = [ws[0], *[w for w in ws[1:] if w not in FILLER_WORDS]] if ws else []
        if pruned:
            ws = pruned
    kept: list[str] = []
    for w in ws:
        cand = "_".join([*kept, w])
        if len(cand) <= limit:
            kept.append(w)
        else:
            break
    # Never end on a dangling preposition
    while len(kept) > 1 and kept[-1] in TRAILING_PREPS:
        kept.pop()
    return "_".join(kept) if kept else "_".join(ws)[:limit].rstrip("_")


def clean_name(tool: Tool, taken: set[str]) -> str:
    """E2 name normalization: acronym-aware snake_case, caller-vs-param disambiguation, no dangling prepositions."""
    raw = tool.op_id or tool.name
    path = tool.route.partition(" ")[2] if tool.route else ""

    # Disambiguate caller-scoped (/me/) vs parameter-scoped (/{userId}/) twins cleanly
    if not tool.operations and re.search(r"/(me|self|current)/", path, re.I):
        tail = [w for p in path.split("/")[-1:] for w in words(p) if not p.startswith("{")]
        ws = ["list", "my", *tail] if tail else ["list", "my", *words(raw)]
    elif not tool.operations and re.search(r"/\{[^}]+\}/[^/{}]+$", path) and re.search(r"\d$", raw):
        # e.g. GET /api/Ansatte/{ansattId}/Grupper with operationId Ansatte_GetGrupper2
        parts = [p for p in path.split("/") if p and p.lower() != "api"]
        parent = words(parts[-3]) if len(parts) >= 3 else []
        leaf = words(parts[-1])
        ws = ["list", *parent, *leaf]
    elif not tool.operations and len(raw) >= openapi.NAME_LIMIT and tool.route:
        # OperationId hit or exceeded the 56-char limit; derive clean name from route
        ws = _from_route(tool)
    elif "_" in raw and not tool.operations:
        ctrl, _, action = raw.partition("_")
        ctrl_ws = words(ctrl)
        act_ws = words(action)
        ctrl_stems = {_stem(w) for w in ctrl_ws}
        act_stems = {_stem(w) for w in act_ws}
        if act_ws and (ctrl_stems & act_stems):
            ws = act_ws
        else:
            ws = [*act_ws[:1], *ctrl_ws, *act_ws[1:]] if act_ws else ctrl_ws
    else:
        ws = words(raw)

    # Normalize verbs and strip implementation-only noise words
    if ws and ws[0] == "opprett":
        ws[0] = "create"
    elif ws and ws[0] == "hent":
        ws[0] = "get"
    elif ws and ws[0] in ("paginert", "paginated"):
        ws[0] = "list"

    dedup: list[str] = []
    for w in ws:
        if len(dedup) >= 1 and w in FILLER_WORDS and not tool.operations:
            continue
        if not dedup or _stem(dedup[-1]) != _stem(w):
            dedup.append(w)

    # Strip trailing filler/preposition words
    while len(dedup) > 1 and dedup[-1] in TRAILING_PREPS:
        dedup.pop()

    base = _trim_words(dedup or ["tool"], NAME_MAX)
    name = base
    n = 2
    while name in taken:
        suffix = f"_{n}"
        name = f"{base[: NAME_MAX - len(suffix)].rstrip('_')}{suffix}"
        n += 1
    taken.add(name)
    return name


def _item_fields(tool: Tool) -> list[str]:
    """Return meaningful domain fields from output_schema, excluding pagination wrapper fields."""
    return [f for f in output_fields(tool.output_schema) if f.lower() not in PAGING_FIELDS]


def _short_purpose(tool: Tool) -> str:
    """Concise noun/gerund phrase describing what a tool does, for clean E1 boundary clauses."""
    if tool.operations:
        vals = ", ".join(list(tool.operations)[:4])
        more = ", ..." if len(tool.operations) > 4 else ""
        return f"{tool.selector} lookups ({vals}{more})"
    desc = " ".join((tool.description or "").split())
    if desc and not desc.startswith("Executes "):
        first = re.split(r"(?<=[.!?])\s", desc)[0].rstrip(".")
        return first[0].lower() + first[1:] if len(first) <= 85 else first[:82].rstrip() + "..."
    ws = words(tool.name)
    noun = " ".join(ws[1:]) or tool.name
    if ws and ws[0] == "create":
        return f"creating {noun}"
    if ws and ws[0] == "update":
        return f"updating {noun}"
    if ws and ws[0] == "delete":
        return f"deleting {noun}"
    if ws and ws[0] == "list":
        return f"listing {noun}"
    if tool.required:
        return f"fetching {noun} by {', '.join(tool.required[:2])}"
    return f"fetching {noun}"


def _base_description(tool: Tool) -> str:
    """Schema-grounded description scaffold replacing 'Executes GET /api/...' stubs (always draft/untested)."""
    desc = " ".join((tool.description or "").split())
    if desc and not desc.startswith("Executes "):
        return desc
    if tool.operations:
        vals = ", ".join(tool.operations)
        return f"Consolidated read tool; set `{tool.selector}` to one of: {vals}."
    ws = words(tool.name)
    verb = ws[0] if ws else ("create" if tool.is_write else "get")
    noun = " ".join(ws[1:]) or tool.name
    action_map = {"get": "Gets", "list": "Lists", "create": "Creates", "update": "Updates", "delete": "Deletes"}
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
    ret = _item_fields(tool)
    if ret:
        parts.append(f"Returns: {', '.join(ret[:6])}.")
    return " ".join(parts)


def infer_chains(tools: list[Tool]) -> dict[str, list[str]]:
    """Infer list -> detail ID-resolution chains from required *Id params matching list/view/reference tools."""
    chains: dict[str, list[str]] = defaultdict(list)
    list_like = [t for t in tools if not t.is_write and (t.operations or words(t.name)[:1] == ["list"])]
    list_stems = {t.name: _domain_stems(t) for t in list_like}
    for t in tools:
        if t.is_write or t.operations or not t.required:
            continue
        id_params = [p for p in t.required if p.lower().endswith(("id", "referanse", "ref"))]
        if not id_params:
            continue
        t_stems = _domain_stems(t)
        for lt in list_like:
            if lt.name == t.name:
                continue
            if t_stems & list_stems[lt.name]:
                # Check if lt is a merged tool and name the exact selector value when obvious
                if lt.operations:
                    match_val = next(
                        (v for v in lt.operations if {_stem(w) for w in words(v)} & t_stems),
                        next(iter(lt.operations)),
                    )
                    chains[t.name].append(f"`{lt.name}({lt.selector}='{match_val}')`")
                else:
                    chains[t.name].append(f"`{lt.name}`")
    return {k: list(dict.fromkeys(v))[:2] for k, v in chains.items()}


def infer_enrichments(tools: list[Tool], plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Propose opt-in foreign-key ID -> name joins against reference_data lookup tools."""
    ref = (plan.get("tools") or {}).get("reference_data")
    if not ref:
        return []
    kinds = list((ref.get("operations") or {}).keys())
    kind_norm = {k: _stem("".join(words(k))) for k in kinds}
    proposals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for t in tools:
        for fld in _item_fields(t):
            if not fld.endswith("Id") or len(fld) <= 3:
                continue
            f_norm = _stem("".join(words(fld[:-2])))
            for kind, k_norm in kind_norm.items():
                if f_norm and (f_norm == k_norm or (len(f_norm) > 4 and f_norm in k_norm)) and (fld, kind) not in seen:
                    seen.add((fld, kind))
                    proposals.append({"field": fld, "lookup": f"reference_data(kind='{kind}')", "enabled": False})
    return proposals


def static_siblings(tools: list[Tool]) -> tuple[dict[str, list[Tool]], list[tuple[str, str, str]]]:
    """Identify same-effect sibling pairs (read<->read, write<->write) from domain stems and response-field overlap.

    Returns (siblings_by_tool, probe_candidates) where probe_candidates are pairs queued for misroute discover.
    """
    stems = {t.name: _domain_stems(t) for t in tools}
    fields_map = {t.name: {f.lower() for f in _item_fields(t) if f.lower() != "id"} for t in tools}
    sibs: dict[str, list[tuple[float, Tool]]] = defaultdict(list)
    probe: list[tuple[str, str, str]] = []
    for i, a in enumerate(tools):
        for b in tools[i + 1 :]:
            if a.is_write != b.is_write:
                continue  # models rarely confuse reads with writes
            shared_stems = stems[a.name] & stems[b.name]
            fa, fb = fields_map[a.name], fields_map[b.name]
            jaccard = len(fa & fb) / len(fa | fb) if (fa and fb) else 0.0
            score = len(shared_stems) * 2.0 + (jaccard * 3.0 if jaccard >= 0.35 else 0.0)
            if score > 0:
                sibs[a.name].append((score, b))
                sibs[b.name].append((score, a))
                why = "domain stem overlap" if shared_stems else f"response schema overlap ({jaccard:.0%})"
                probe.append((a.name, b.name, why))
    out: dict[str, list[Tool]] = {}
    for name, rows in sibs.items():
        rows.sort(key=lambda x: (-x[0], x[1].name))
        out[name] = [t for _, t in rows[:2]]
    return out, probe


def apply_e1_e2(
    tools: list[Tool], excluded: dict[str, str] | None = None
) -> tuple[list[Tool], dict[str, str], dict[str, Any]]:
    """Apply E2 (clean sibling-distinct naming) and E1 (same-effect boundaries + list->detail pointers).

    Returns (polished_tools, old_to_new_name_map, diagnostics) where diagnostics includes probe_candidates and
    needs_human_name flags. All generated descriptions are static drafts until confirmed with misroute check.
    """
    taken: set[str] = set()
    rename_map: dict[str, str] = {}
    renamed: list[Tool] = []
    for t in tools:
        new_name = clean_name(t, taken)
        rename_map[t.name] = new_name
        renamed.append(replace(t, name=new_name))

    needs_human = [t.name for t in renamed if re.search(r"(?:_|^)\d+$|[a-z]\d+$", t.name)]
    grounded = [replace(t, description=_base_description(t)) for t in renamed]
    sib_map, probe_candidates = static_siblings(grounded)
    chains = infer_chains(grounded)

    # Note excluded binary attachment downloads on attachment-listing tools
    excluded = excluded or {}
    binary_attach_excluded = any("attachment" in k.lower() or "vedlegg" in k.lower() for k in excluded)

    final: list[Tool] = []
    for t in grounded:
        extra: list[str] = []
        if t.name in chains:
            extra.append(f"To find IDs, call {' or '.join(chains[t.name])} first.")
        for s in sib_map.get(t.name) or []:
            if s.name in " ".join(chains.get(t.name, [])):
                continue
            extra.append(f"Use `{s.name}` for {_short_purpose(s)}.")
        if binary_attach_excluded and ("attachment" in t.name or "vedlegg" in t.name):
            extra.append("Returns metadata only; raw file download endpoints are excluded.")
        desc = f"{t.description} {' '.join(extra)}".strip() if extra else t.description
        final.append(replace(t, description=desc))
    return final, rename_map, {"probe_candidates": probe_candidates, "needs_human_name": needs_human}


def propose_clean(tools: list[Tool], doc: dict | None = None, split_audiences: bool = True) -> dict[str, Any]:
    """Propose a clean catalog plan: split parallel audiences, drop singletons from reference_data, flag binaries."""
    base = merge.propose(tools, doc)
    exclude = dict(base.get("exclude") or {})
    suspicious_binary: dict[str, str] = {}
    for name, why in list(exclude.items()):
        if "returns a file" in why and SUSPICIOUS_BINARY.search(name):
            suspicious_binary[name] = (
                "declared application/octet-stream; verify whether it returns JSON/GeoJSON before excluding"
            )
            exclude[name] = suspicious_binary[name]

    audience_excluded = parallel_audiences(tools) if split_audiences else {}
    for name, why in audience_excluded.items():
        exclude.setdefault(name, why)

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

    plan: dict[str, Any] = {
        "note": base.get("note", merge.NOTE),
        "audience": "primary (internal); parallel secondary surfaces excluded" if audience_excluded else "all",
        "tools": cleaned_tools,
        "exclude": exclude,
    }
    enrichments = infer_enrichments(tools, plan)
    if enrichments:
        plan["enrich"] = enrichments
    return plan
