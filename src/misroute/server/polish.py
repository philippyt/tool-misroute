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
EXTERNAL_REF = re.compile(r"(ekstern|external|referanse|reference)", re.I)
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
COMMON_OVERLAP_FIELDS = frozenset(
    {
        "id",
        "navn",
        "name",
        "title",
        "tittel",
        "beskrivelse",
        "description",
        "code",
        "kode",
        "type",
        "status",
    }
)
TIMESTAMP_FIELD = re.compile(r"^(datotid|created|updated|modified|timestamp)", re.I)
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
        w = w[:-3]
    elif len(w) > 4 and w.endswith(("er", "es")):
        w = w[:-2]
    elif len(w) > 4 and w.endswith(("r", "s")):
        w = w[:-1]
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


def _singular(word: str) -> str:
    w = word.lower()
    if len(w) > 5 and w.endswith("er") and not w.endswith(("ser", "ter")):
        return w[:-1]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _is_collection_return(tool: Tool) -> bool:
    schema = tool.output_schema or {}
    if schema.get("type") == "array" or "items" in schema:
        return True
    props = schema.get("properties") or {}
    return any(isinstance(v, dict) and (v.get("type") == "array" or "items" in v) for v in props.values())


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
        return not _is_collection_return(tool)
    return False


def _from_route(tool: Tool) -> list[str]:
    """Fallback word sequence derived from HTTP method + route path when operationId is truncated/noisy."""
    method, _, path = tool.route.partition(" ")
    segs = [p for p in path.split("/") if p and p.lower() != "api" and not re.fullmatch(r"v\d+", p, re.I)]
    ws: list[str] = []
    if method == "POST":
        ws.append("list" if tool.read_only else "create")
    elif method in ("PUT", "PATCH"):
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
    # Singularize trailing plural resource noun on single-item get/create/update/delete
    if ws and ws[0] in ("get", "create", "update", "delete") and len(ws) > 1:
        ws[-1] = _singular(ws[-1])
    return ws


def _trim_words(ws: list[str], limit: int = NAME_MAX) -> str:
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
    while len(kept) > 1 and kept[-1] in TRAILING_PREPS:
        kept.pop()
    return "_".join(kept) if kept else "_".join(ws)[:limit].rstrip("_")


def clean_name(tool: Tool, taken: set[str]) -> str:
    """E2 name normalization: acronym-aware snake_case, caller/param & parent/child aware, list vs get consistent."""
    raw = tool.op_id or tool.name
    path = tool.route.partition(" ")[2] if tool.route else ""
    segs = [p for p in path.split("/") if p and p.lower() != "api" and not re.fullmatch(r"v\d+", p, re.I)]

    # 1. Caller-scoped (/me/) vs parameter-scoped (/{userId}/) twins
    if not tool.operations and re.search(r"/(me|self|current)/", path, re.I):
        tail = [w for p in path.split("/")[-1:] for w in words(p) if not p.startswith("{")]
        ws = ["list", "my", *tail] if tail else ["list", "my", *words(raw)]
    elif not tool.operations and re.search(r"/\{[^}]+\}/[^/{}]+$", path) and re.search(r"\d$", raw):
        parent = words(segs[-3]) if len(segs) >= 3 else []
        leaf = words(segs[-1])
        ws = ["list", *parent, *leaf]
    # 2. Parent-child route like GET /api/Arbeidsordrer/{id}/Skjema where op_id is Skjema_GetById
    elif (
        not tool.operations
        and len(segs) == 3
        and segs[1].startswith("{")
        and not segs[2].startswith("{")
        and re.search(r"by_?id$", raw, re.I)
    ):
        parent_ws = words(segs[0])
        if parent_ws:
            parent_ws[-1] = _singular(parent_ws[-1])
        ws = ["get", *parent_ws, *words(segs[2])]
    # 3. OperationId hit/exceeded 56-char limit; derive clean name from route
    elif not tool.operations and len(raw) >= openapi.NAME_LIMIT and tool.route:
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

    # Normalize verbs
    if ws and ws[0] == "opprett":
        ws[0] = "create"
    elif ws and ws[0] == "hent":
        ws[0] = "get"
    elif ws and ws[0] in ("paginert", "paginated"):
        ws[0] = "list"

    # Collection-returning GET without a path {id} should use list_, not get_
    has_path_id = any(s.startswith("{") for s in segs)
    if (
        not tool.operations
        and not tool.is_write
        and not has_path_id
        and _is_collection_return(tool)
        and ws[:1] == ["get"]
    ):
        ws[0] = "list"

    dedup: list[str] = []
    for w in ws:
        if len(dedup) >= 1 and w in FILLER_WORDS and not tool.operations:
            continue
        if not dedup or _stem(dedup[-1]) != _stem(w):
            dedup.append(w)

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
    """Return meaningful item fields from output_schema; only strip an array field when next to paging fields."""
    raw = output_fields(tool.output_schema)
    schema = tool.output_schema or {}
    props = schema.get("properties") or {}
    has_paging = any(k.lower() in PAGING_FIELDS for k in props)
    wrapper_arrays = (
        {k.lower() for k, v in props.items() if isinstance(v, dict) and (v.get("type") == "array" or "items" in v)}
        if has_paging
        else set()
    )
    return [f for f in raw if f.lower() not in PAGING_FIELDS and f.lower() not in wrapper_arrays]


def _short_purpose(tool: Tool) -> str:
    """Concise noun/gerund phrase describing what a tool does, never cutting mid-sentence."""
    if tool.operations:
        vals = ", ".join(list(tool.operations)[:4])
        more = ", ..." if len(tool.operations) > 4 else ""
        return f"{tool.selector} lookups ({vals}{more})"
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
        return desc if desc.endswith((".", "!", "?")) else desc + "."
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


def _norm_entity(text: str) -> str:
    return _stem("".join(words(text)))


def _entity_of_id_param(tool: Tool, param: str) -> str | None:
    """Resolve the full compound entity of a required ID parameter, or None for external references."""
    if EXTERNAL_REF.search(param):
        return None
    if param.lower().endswith("id") and len(param) > 2:
        return _norm_entity(param[:-2])
    if param.lower() == "id" and tool.route:
        path = tool.route.partition(" ")[2]
        segs = [p for p in path.split("/") if p]
        for i, seg in enumerate(segs):
            if seg.lower() == "{id}" and i > 0:
                return _norm_entity(segs[i - 1])
    return None


def _requires_id(tool: Tool) -> bool:
    return any(p.lower() == "id" or p.lower().endswith("id") for p in tool.required)


def _source_entities(tool: Tool) -> list[tuple[str, str]]:
    """Return [(compound_entity, call_hint)] for zero-required-ID read tools that can supply IDs."""
    if tool.is_write or _requires_id(tool):
        return []
    if tool.operations:
        # reference_data: each kind is its own entity
        if tool.name == "reference_data":
            return [(_norm_entity(k), f"`{tool.name}({tool.selector}='{k}')`") for k in tool.operations]
        # *_view: entity is the prefix before _view, prefer paginert/list selector
        base = re.sub(r"_(view|by_parent)$", "", tool.name)
        pref = next((v for v in tool.operations if v in ("paginert", "list", "all")), next(iter(tool.operations)))
        return [(_norm_entity(base), f"`{tool.name}({tool.selector}='{pref}')`")]
    ws = words(tool.name)
    if ws and ws[0] == "list":
        noun = "_".join(w for w in ws[1:] if w not in ("my", "all", "alle"))
        if noun:
            return [(_norm_entity(noun), f"`{tool.name}`")]
    return []


def infer_chains(tools: list[Tool]) -> dict[str, list[str]]:
    """Infer exact compound-entity list -> detail chains, rejecting downstream tools that require an ID."""
    sources: dict[str, list[str]] = defaultdict(list)
    for t in tools:
        for ent, call_hint in _source_entities(t):
            if ent:
                sources[ent].append(call_hint)

    chains: dict[str, list[str]] = defaultdict(list)
    for t in tools:
        if t.is_write or t.operations or not t.required:
            continue
        for param in t.required:
            ent = _entity_of_id_param(t, param)
            if not ent:
                continue
            for call_hint in sources.get(ent, []):
                if f"`{t.name}`" != call_hint and call_hint not in chains[t.name]:
                    chains[t.name].append(call_hint)
    return {k: v[:2] for k, v in chains.items()}


def infer_enrichments(tools: list[Tool], plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Propose opt-in foreign-key ID -> name joins against reference_data lookup tools."""
    ref = (plan.get("tools") or {}).get("reference_data")
    if not ref:
        return []
    kinds = list((ref.get("operations") or {}).keys())
    kind_norm = {k: _norm_entity(k) for k in kinds}
    proposals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for t in tools:
        for fld in _item_fields(t):
            if not fld.endswith("Id") or len(fld) <= 3:
                continue
            f_norm = _norm_entity(fld[:-2])
            for kind, k_norm in kind_norm.items():
                if f_norm and (f_norm == k_norm or (len(f_norm) > 4 and f_norm in k_norm)) and (fld, kind) not in seen:
                    seen.add((fld, kind))
                    proposals.append({"field": fld, "lookup": f"reference_data(kind='{kind}')", "enabled": False})
    return proposals


def _informative_fields(tool: Tool, source_by_name: dict[str, Tool] | None = None) -> set[str]:
    """Non-generic response fields for schema-overlap scoring, including member ops of merged tools."""
    flds = list(_item_fields(tool))
    if tool.operations and source_by_name:
        for op_name in tool.operations.values():
            if op_name in source_by_name:
                flds.extend(_item_fields(source_by_name[op_name]))
    return {f.lower() for f in flds if f.lower() not in COMMON_OVERLAP_FIELDS and not TIMESTAMP_FIELD.match(f)}


def static_siblings(
    tools: list[Tool],
    chains: dict[str, list[str]] | None = None,
    source_tools: list[Tool] | None = None,
) -> tuple[dict[str, list[Tool]], list[tuple[str, str, str]]]:
    """Identify same-effect sibling pairs from domain stems and non-generic response-field overlap.

    Skips pairs already linked by a list -> detail chain. Returns (siblings_by_tool, probe_candidates)
    sorted with schema-overlap pairs first.
    """
    chains = chains or {}
    source_by = {t.name: t for t in source_tools} if source_tools else None
    stems = {t.name: _domain_stems(t) for t in tools}
    fields_map = {t.name: _informative_fields(t, source_by) for t in tools}
    sibs: dict[str, list[tuple[float, Tool]]] = defaultdict(list)
    probe: list[tuple[int, str, str, str]] = []
    for i, a in enumerate(tools):
        for b in tools[i + 1 :]:
            if a.is_write != b.is_write:
                continue
            # Skip pairs already linked as a list -> detail chain
            if any(b.name in h for h in chains.get(a.name, [])) or any(a.name in h for h in chains.get(b.name, [])):
                continue
            shared_stems = stems[a.name] & stems[b.name]
            fa, fb = fields_map[a.name], fields_map[b.name]
            shared_fields = fa & fb
            jaccard = len(shared_fields) / len(fa | fb) if (fa and fb) else 0.0
            has_schema = len(shared_fields) >= 2 and jaccard >= 0.25
            score = len(shared_stems) * 2.0 + (jaccard * 3.0 if has_schema else 0.0)
            if score > 0:
                sibs[a.name].append((score, b))
                sibs[b.name].append((score, a))
                if has_schema and not shared_stems:
                    probe.append((0, a.name, b.name, f"response schema overlap ({jaccard:.0%})"))
                elif has_schema:
                    probe.append((1, a.name, b.name, f"stem + schema overlap ({jaccard:.0%})"))
                else:
                    probe.append((2, a.name, b.name, "domain stem overlap"))
    out: dict[str, list[Tool]] = {}
    for name, rows in sibs.items():
        rows.sort(key=lambda x: (-x[0], x[1].name))
        out[name] = [t for _, t in rows[:2]]
    probe.sort(key=lambda x: (x[0], x[1], x[2]))
    return out, [(a, b, why) for _, a, b, why in probe]


def apply_e1_e2(
    tools: list[Tool],
    excluded: dict[str, str] | None = None,
    source_tools: list[Tool] | None = None,
) -> tuple[list[Tool], dict[str, str], dict[str, Any]]:
    """Apply E2 (clean sibling-distinct naming) and E1 (same-effect boundaries + exact list->detail pointers)."""
    taken: set[str] = set()
    rename_map: dict[str, str] = {}
    renamed: list[Tool] = []
    for t in tools:
        new_name = clean_name(t, taken)
        rename_map[t.name] = new_name
        renamed.append(replace(t, name=new_name))

    needs_human = [t.name for t in renamed if re.search(r"(?:_|^)\d+$|[a-z]\d+$", t.name)]
    grounded = [replace(t, description=_base_description(t)) for t in renamed]
    chains = infer_chains(grounded)
    sib_map, probe_candidates = static_siblings(grounded, chains=chains, source_tools=source_tools)

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
        base_desc = t.description.rstrip()
        if base_desc and not base_desc.endswith((".", "!", "?")):
            base_desc += "."
        desc = f"{base_desc} {' '.join(extra)}".strip() if extra else base_desc
        final.append(replace(t, description=desc))
    return final, rename_map, {"probe_candidates": probe_candidates, "needs_human_name": needs_human}


def propose_clean(tools: list[Tool], doc: dict | None = None, split_audiences: bool = True) -> dict[str, Any]:
    """Propose a clean catalog plan: split parallel audiences, drop singletons from reference_data, flag binaries."""
    base = merge.propose(tools, doc)
    exclude = dict(base.get("exclude") or {})
    for name, why in list(exclude.items()):
        if "returns a file" in why and SUSPICIOUS_BINARY.search(name):
            exclude[name] = "declared application/octet-stream; verify whether it returns JSON/GeoJSON before excluding"

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
