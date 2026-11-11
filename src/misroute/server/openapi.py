import json
import re
from pathlib import Path
from typing import Any
import yaml
from misroute.server.catalog import Tool

METHODS = ("get", "post", "put", "patch", "delete")
READ_POSTS = ("paginert", "count", "search", "sok", "query", "filter", "list", "liste")
NAME_LIMIT = 56
READ_VERBS = ("get", "list", "count", "search", "find", "hent", "finn", "query", "paginert")


def reads(method: str, route: str, op: dict) -> bool:
    action = str(op.get("operationId", "")).split("_")[-1].lower()
    return method == "get" or (
        method == "post" and (action.startswith(READ_VERBS) or any(w in route.lower() for w in READ_POSTS))
    )


FIXES = "x-misroute-fixes"
SCHEMA_KEYS = ("type", "format", "enum", "items", "default", "minimum", "maximum", "minLength", "maxLength", "pattern")
FLOWS = {"accessCode": "authorizationCode", "application": "clientCredentials"}
REFS = (
    ("#/definitions/", "#/components/schemas/"),
    ("#/parameters/", "#/components/parameters/"),
    ("#/responses/", "#/components/responses/"),
)
TRANSPORT_HEADERS = ("authorization", "cookie", "accept", "content-type")


def load(path: str | Path) -> dict[str, Any]:
    """The spec as OpenAPI 3: a Swagger 2.0 file is converted, and problems that stop converters are repaired.
    What was changed is listed under x-misroute-fixes, empty when the file was used as it is."""
    text = Path(path).read_text(encoding="utf-8-sig")
    doc = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    fixed = []
    if str(doc.get("swagger", "")).startswith("2"):
        doc = upgrade(doc)
        fixed.append("converted from Swagger 2.0 to OpenAPI 3.0")
    doc[FIXES] = fixed + repair(doc)
    return doc


def moved(node: Any) -> Any:
    """Swagger 2.0 references pointed at where the same things live in OpenAPI 3."""
    if isinstance(node, dict):
        out = {k: moved(v) for k, v in node.items()}
        ref = out.get("$ref")
        if isinstance(ref, str):
            out["$ref"] = next((new + ref[len(old) :] for old, new in REFS if ref.startswith(old)), ref)
        return out
    return [moved(v) for v in node] if isinstance(node, list) else node


def schema_of(p: dict) -> dict[str, Any]:
    if p.get("type") == "file":
        return {"type": "string", "format": "binary"}
    return {k: p[k] for k in SCHEMA_KEYS if k in p}


def upgrade(doc: dict) -> dict[str, Any]:
    """A Swagger 2.0 document as OpenAPI 3.0: definitions become component schemas, a body parameter and form
    fields become the request body, parameter types move under schema, response schemas move under content, and
    host, basePath and securityDefinitions become servers and security schemes."""
    shared = doc.get("parameters") or {}
    answers = doc.get("responses") or {}

    def whole(node: dict, known: dict) -> dict:
        ref = node.get("$ref", "") if isinstance(node, dict) else ""
        return known.get(ref.rsplit("/", 1)[-1], node) if ref.startswith("#/") else node

    def kind(kinds: list[str]) -> str:
        return next((k for k in kinds if "json" in k), kinds[0])

    def operation(op: dict, inherited: list) -> dict:
        params = [whole(p, shared) for p in [*inherited, *op.get("parameters", [])]]
        new = {k: v for k, v in op.items() if k not in ("parameters", "consumes", "produces", "responses")}
        takes = op.get("consumes") or doc.get("consumes") or ["application/json"]
        gives = op.get("produces") or doc.get("produces") or ["application/json"]
        new["parameters"] = [
            {
                "name": p["name"],
                "in": p["in"],
                "required": bool(p.get("required")),
                "description": p.get("description", ""),
                "schema": schema_of(p),
            }
            for p in params
            if p.get("in") not in ("body", "formData")
        ]
        body = next((p for p in params if p.get("in") == "body"), None)
        form = [p for p in params if p.get("in") == "formData"]
        if body:
            content = {kind(takes): {"schema": body.get("schema") or {}}}
            new["requestBody"] = {"required": bool(body.get("required")), "content": content}
        elif form:
            files = any(p.get("type") == "file" for p in form) or "multipart/form-data" in takes
            fields = {p["name"]: {**schema_of(p), "description": p.get("description", "")} for p in form}
            shape = {"type": "object", "properties": fields, "required": [p["name"] for p in form if p.get("required")]}
            sent = "multipart/form-data" if files else "application/x-www-form-urlencoded"
            new["requestBody"] = {"required": bool(shape["required"]), "content": {sent: {"schema": shape}}}
        new["responses"] = {}
        for code, resp in (op.get("responses") or {}).items():
            resp = whole(resp, answers)
            made: dict[str, Any] = {"description": resp.get("description", "")}
            if resp.get("schema"):
                made["content"] = {kind(gives): {"schema": resp["schema"]}}
            new["responses"][str(code)] = made
        return new

    schemes = {}
    for name, old in (doc.get("securityDefinitions") or {}).items():
        if old.get("type") == "basic":
            schemes[name] = {"type": "http", "scheme": "basic"}
        elif old.get("type") == "oauth2":
            flow = {k: old[k] for k in ("authorizationUrl", "tokenUrl") if k in old}
            flow["scopes"] = old.get("scopes") or {}
            schemes[name] = {
                "type": "oauth2",
                "flows": {FLOWS.get(old.get("flow", ""), old.get("flow", "implicit")): flow},
            }
        else:
            schemes[name] = dict(old)
    out: dict[str, Any] = {"openapi": "3.0.3", "info": doc.get("info") or {"title": "API", "version": "1"}}
    if doc.get("host"):
        out["servers"] = [{"url": f"{(doc.get('schemes') or ['https'])[0]}://{doc['host']}{doc.get('basePath', '')}"}]
    elif doc.get("basePath"):
        out["servers"] = [{"url": doc["basePath"]}]
    out["paths"] = {
        route: {m: operation(item[m], item.get("parameters", [])) for m in METHODS if item.get(m)}
        for route, item in (doc.get("paths") or {}).items()
    }
    out["components"] = {"schemas": doc.get("definitions") or {}, "securitySchemes": schemes}
    if doc.get("security"):
        out["security"] = doc["security"]
    return moved(out)


REF_DEPTH = 6


def resolve(doc: dict, node: Any, trail: tuple[str, ...] = ()) -> Any:
    """The node with every reference replaced by what it points at. A reference that leads back to itself, or
    a chain of more than REF_DEPTH references, ends in an empty schema. Nesting alone never cuts a schema."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in trail or len(trail) >= REF_DEPTH:
                return {}
            target: Any = doc
            for part in ref.lstrip("#/").split("/"):
                target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
            return resolve(doc, target, (*trail, ref))
        return {k: resolve(doc, v, trail) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(doc, v, trail) for v in node]
    return node


def slug(text: str) -> str:
    text = re.sub(r"[\s\-.]+", "_", text)
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "", text)).strip("_")[:NAME_LIMIT]


def name_of(method: str, path: str, op: dict) -> str:
    """The tool name FastMCP gives an operation: the operationId, or method and path, slugified to 56 characters."""
    if op.get("operationId"):
        return slug(op["operationId"].split("__")[0])
    return slug(op.get("summary") or f"{method.upper()}_{path}")


def repair(doc: dict) -> list[str]:
    """Fix spec problems that stop converters from loading it or from building a working call. Returns what was
    changed. A path parameter spelled differently from its placeholder in the route is renamed to the
    placeholder, since the value could not be filled in otherwise."""
    fixed = []
    for name, scheme in ((doc.get("components") or {}).get("securitySchemes") or {}).items():
        for flow, body in (scheme.get("flows") or {}).items():
            if isinstance(body, dict) and "scopes" not in body:
                body["scopes"] = {}
                fixed.append(f"securitySchemes.{name}.flows.{flow} had no scopes")
    for route, item in (doc.get("paths") or {}).items():
        holes = {h.lower(): h for h in re.findall(r"{([^{}]+)}", route)}
        for method in METHODS:
            for p in [*item.get("parameters", []), *((item.get(method) or {}).get("parameters") or [])]:
                right = holes.get(str(p.get("name", "")).lower()) if p.get("in") == "path" else None
                if right and right != p["name"]:
                    fixed.append(f"{method.upper()} {route}: path parameter {p['name']} renamed to {right}")
                    p["name"] = right
    return fixed


def secrets(doc: dict) -> set[str]:
    """Header and cookie names that carry credentials or transport details, which the server sends and an agent
    must never be asked to fill in."""
    schemes = ((doc.get("components") or {}).get("securitySchemes") or {}).values()
    named = {str(x.get("name", "")).lower() for x in schemes if x.get("type") == "apiKey"}
    return {*named, *TRANSPORT_HEADERS} - {""}


def merged(schema: Any) -> Any:
    """An object schema with its allOf parts folded in, so inherited fields are visible as arguments."""
    if not isinstance(schema, dict) or "allOf" not in schema:
        return schema
    out = {k: v for k, v in schema.items() if k != "allOf"}
    props, required = dict(out.get("properties") or {}), list(out.get("required") or [])
    for part in schema["allOf"]:
        part = merged(part)
        if isinstance(part, dict):
            props = {**(part.get("properties") or {}), **props}
            required += [r for r in part.get("required") or [] if r not in required]
    return {**out, "type": "object", "properties": props, "required": required}


def uploads(body: dict) -> list[str]:
    return [k for k, v in (body.get("properties") or {}).items() if "binary" in json.dumps(v)]


def args(doc: dict, shared: list, op: dict) -> dict[str, Any]:
    """Input schema with an object request body flattened into top level arguments, as FastMCP does. A parameter
    whose name a body field also uses becomes name__location."""
    hidden = secrets(doc)
    params = [
        p
        for p in resolve(doc, shared + op.get("parameters", []))
        if not (p.get("in") in ("header", "cookie") and str(p.get("name", "")).lower() in hidden)
    ]
    content = resolve(doc, (op.get("requestBody") or {}).get("content", {}))
    body = merged((best(content).get("schema") if content else None) or {})
    fields = body.get("properties") if body.get("type", "object") == "object" and body.get("properties") else None
    props: dict[str, Any] = {}
    required: list[str] = []
    for p in params:
        key = f"{p['name']}__{p.get('in', 'query')}" if fields and p["name"] in fields else p["name"]
        props[key] = {**(p.get("schema") or {}), "description": p.get("description", "")}
        if p.get("required"):
            required.append(key)
    if fields:
        props.update(fields)
        required += [r for r in body.get("required", []) if r in fields]
    elif body:
        props["body"] = body
        if (op.get("requestBody") or {}).get("required"):
            required.append("body")
    return {"type": "object", "properties": props, "required": required}


def best(content: dict) -> dict:
    """The JSON form of a body or a reply when there is one, else the first form listed."""
    return next((v for k, v in content.items() if "json" in k), next(iter(content.values()), {})) or {}


def tools(path: str | Path) -> list[Tool]:
    """Tools as FastMCP exposes them. Without a description or summary the description is Executes METHOD path,
    FastMCP's fallback."""
    doc = load(path)
    out = []
    for route, item in (doc.get("paths") or {}).items():
        shared = item.get("parameters", [])
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            text = (op.get("description") or op.get("summary") or "").strip()
            out.append(
                Tool(
                    name=name_of(method, route, op),
                    description=text or f"Executes {method.upper()} {route}",
                    input_schema=args(doc, shared, op),
                    output_schema=returns(doc, op),
                    read_only=reads(method, route, op),
                    destructive=method == "delete",
                    route=f"{method.upper()} {route}",
                    op_id=op.get("operationId", ""),
                )
            )
    return out


def returns(doc: dict, op: dict) -> dict[str, Any] | None:
    for code, resp in (op.get("responses") or {}).items():
        if str(code).startswith("2"):
            content = resolve(doc, (resp or {}).get("content", {}))
            schema = best(content).get("schema") if content else None
            if schema:
                return merged(schema)
    return None


def binary_only(doc: dict, op: dict) -> bool:
    """Every documented success reply is a file, so an agent gets nothing it can read."""
    found = [
        resolve(doc, (resp or {}).get("content", {}))
        for code, resp in (op.get("responses") or {}).items()
        if str(code).startswith("2")
    ]
    kinds = [k for content in found for k in content]
    return bool(kinds) and not any("json" in k or k.startswith("text/") for k in kinds)


def operations(doc: dict) -> dict[str, dict]:
    """Each operation by its METHOD /path route."""
    return {
        f"{m.upper()} {route}": item[m]
        for route, item in (doc.get("paths") or {}).items()
        for m in METHODS
        if item.get(m)
    }
