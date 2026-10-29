import json
import re
from pathlib import Path
from typing import Any
import yaml
from misroute.server.catalog import Tool

METHODS = ("get", "post", "put", "patch", "delete")
READ_POSTS = ("paginert", "count", "search", "sok", "query", "filter", "list", "liste")
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


def name_of(method: str, path: str, op: dict) -> str:
    if op.get("operationId"):
        return re.sub(r"[^A-Za-z0-9_]", "_", op["operationId"])[:64]
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{method}_{path}").strip("_")[:64]


def tools(path: str | Path) -> list[Tool]:
    """Tools as an OpenAPI to MCP converter would expose them. Without a summary the description is the method
    and path, which is the usual converter fallback."""
    doc = load(path)
    out = []
    for route, item in (doc.get("paths") or {}).items():
        shared = item.get("parameters", [])
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            props: dict[str, Any] = {}
            required: list[str] = []
            for p in resolve(doc, shared + op.get("parameters", [])):
                props[p["name"]] = {**(p.get("schema") or {}), "description": p.get("description", "")}
                if p.get("required"):
                    required.append(p["name"])
            body = resolve(doc, (op.get("requestBody") or {}).get("content", {}))
            schema = next(iter(body.values()), {}).get("schema") if body else None
            if schema:
                props["body"] = schema
                required.append("body")
            text = " ".join(x for x in (op.get("summary"), op.get("description")) if x).strip()
            read = reads(method, route, op)
            out.append(
                Tool(
                    name=name_of(method, route, op),
                    description=text or f"{method.upper()} {route}",
                    input_schema={"type": "object", "properties": props, "required": required},
                    read_only=read,
                    destructive=method == "delete",
                )
            )
    return out
