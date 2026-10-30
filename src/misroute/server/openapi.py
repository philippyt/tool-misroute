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


def load(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)


def resolve(doc: dict, node: Any, depth: int = 0) -> Any:
    if depth > 6:
        return {}
    if isinstance(node, dict):
        if "$ref" in node:
            target: Any = doc
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target.get(part, {})
            return resolve(doc, target, depth + 1)
        return {k: resolve(doc, v, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(doc, v, depth + 1) for v in node]
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
    """Fix spec problems that stop converters from loading it. Returns what was changed."""
    fixed = []
    for name, scheme in ((doc.get("components") or {}).get("securitySchemes") or {}).items():
        for flow, body in (scheme.get("flows") or {}).items():
            if isinstance(body, dict) and "scopes" not in body:
                body["scopes"] = {}
                fixed.append(f"securitySchemes.{name}.flows.{flow} had no scopes")
    return fixed


def args(doc: dict, shared: list, op: dict) -> dict[str, Any]:
    """Input schema with an object request body flattened into top level arguments, as FastMCP does. A parameter
    whose name a body field also uses becomes name__location."""
    params = resolve(doc, shared + op.get("parameters", []))
    content = resolve(doc, (op.get("requestBody") or {}).get("content", {}))
    body = (next(iter(content.values()), {}).get("schema") if content else None) or {}
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


def tools(path: str | Path) -> list[Tool]:
    """Tools as FastMCP exposes them. Without a description or summary the description is Executes METHOD path,
    FastMCP's fallback."""
    doc = load(path)
    repair(doc)
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
            schema = next(iter(content.values()), {}).get("schema") if content else None
            if schema:
                return schema
    return None
