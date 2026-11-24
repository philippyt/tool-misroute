import json
import os
from importlib.metadata import version
from pathlib import Path
from typing import Any
import yaml
from misroute.server.openapi import FIXES

FASTMCP = "4.1.4"

SERVER = '''# /// script
# requires-python = ">=3.11"
# dependencies = ["fastmcp=={fastmcp}", "pyyaml"]
# ///
"""FastMCP server for {title} with the descriptions in overlay.yaml, written by misroute {misroute}.

Review overlay.yaml first. Set exclude: true on operations agents should not see. Run with: uv run server.py
API_BASE_URL sets the backend and API_HEADERS takes a JSON object of headers, for example an API key."""
import json
import os
import re
from pathlib import Path
from urllib.parse import quote
import httpx2
import yaml
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers.openapi import MCPType
from fastmcp.tools import Tool, ToolResult
from mcp_types import ToolAnnotations

HERE = Path(__file__).resolve().parent
SPEC = Path(os.environ.get("API_SPEC", HERE / {spec!r}))
OVERLAY = yaml.safe_load((HERE / "overlay.yaml").read_text(encoding="utf-8"))["operations"]
MERGED = json.loads((HERE / "merged.json").read_text(encoding="utf-8")) if (HERE / "merged.json").exists() else {{}}
MAX_RESPONSE_BYTES = int(os.environ.get("MCP_MAX_RESPONSE_BYTES", "32000"))
REDACT_SECRETS = os.environ.get("MCP_REDACT_SECRETS", "1") != "0"
SECRET_KEY_RE = re.compile(
    r"(passord|password|passwd|secret|apikey|api_key|accesstoken|access_token|"
    r"refreshtoken|refresh_token|privatekey|private_key|clientsecret|client_secret|authorization)",
    re.I,
)
SECRET_HEADER_RE = re.compile(r"(authorization|api[-_]?key|token|secret|password|passord|cookie)", re.I)


def redact_secrets(node):
    if isinstance(node, list):
        return [redact_secrets(x) for x in node]
    if not isinstance(node, dict):
        return node
    flagged_secret = node.get("isSecret") is True or node.get("is_secret") is True
    header_name = node.get("headerName") or node.get("name") or node.get("key")
    header_is_secret = isinstance(header_name, str) and bool(SECRET_HEADER_RE.search(header_name))
    out = {{}}
    for k, v in node.items():
        if isinstance(v, str) and v:
            if (flagged_secret or header_is_secret) and k.lower() in ("value", "headervalue", "secret", "token"):
                out[k] = "[REDACTED]"
                continue
            if SECRET_KEY_RE.search(k) and not k.lower().startswith(("is", "has")):
                out[k] = "[REDACTED]"
                continue
        out[k] = redact_secrets(v)
    return out


def cap_payload(data: dict, limit: int) -> dict | None:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(raw) <= limit:
        return None
    for key, val in data.items():
        if isinstance(val, list) and val:
            kept: list = []
            used = len(json.dumps({{**data, key: []}}, ensure_ascii=False).encode("utf-8")) + 256
            for item in val:
                size = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 2
                if used + size > limit:
                    break
                kept.append(item)
                used += size
            return {{
                **data,
                key: kept,
                "_truncated": {{
                    "field": key,
                    "returned_items": len(kept),
                    "total_items": len(val),
                    "max_bytes": limit,
                    "hint": "Response exceeded MCP_MAX_RESPONSE_BYTES; narrow filters or paginate.",
                }},
            }}
    return {{
        "_truncated": True,
        "max_bytes": limit,
        "preview": raw[:limit].decode("utf-8", errors="ignore"),
        "hint": "Response exceeded MCP_MAX_RESPONSE_BYTES; narrow filters or paginate.",
    }}


class CapResponseMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if isinstance(result.structured_content, dict):
            data = redact_secrets(result.structured_content) if REDACT_SECRETS else result.structured_content
            capped = cap_payload(data, MAX_RESPONSE_BYTES) if MAX_RESPONSE_BYTES > 0 else None
            if capped is not None:
                return ToolResult(structured_content=capped, is_error=result.is_error)
            if REDACT_SECRETS and data != result.structured_content:
                return ToolResult(structured_content=data, is_error=result.is_error)
        elif result.content:
            text = getattr(result.content[0], "text", None)
            if isinstance(text, str):
                parsed = None
                try:
                    parsed = json.loads(text)
                except ValueError:
                    pass
                if isinstance(parsed, (dict, list)):
                    cleaned = redact_secrets(parsed) if REDACT_SECRETS else parsed
                    wrapped = cleaned if isinstance(cleaned, dict) else {{"result": cleaned}}
                    if MAX_RESPONSE_BYTES > 0:
                        capped = cap_payload(wrapped, MAX_RESPONSE_BYTES)
                        if capped is not None:
                            return ToolResult(structured_content=capped, is_error=result.is_error)
                    if REDACT_SECRETS and cleaned != parsed:
                        return ToolResult(structured_content=wrapped, is_error=result.is_error)
                elif MAX_RESPONSE_BYTES > 0 and len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    cut = text.encode("utf-8")[:MAX_RESPONSE_BYTES].decode("utf-8", errors="ignore")
                    note = f"\\n... [truncated at {{MAX_RESPONSE_BYTES}} bytes; narrow filters or paginate]"
                    return ToolResult(content=cut + note, is_error=result.is_error)
        return result


def load_spec() -> dict:
    text = SPEC.read_text(encoding="utf-8")
    spec = json.loads(text) if text.lstrip().startswith("{{") else yaml.safe_load(text)
    for scheme in (spec.get("components") or {{}}).get("securitySchemes", {{}}).values():
        for flow in (scheme.get("flows") or {{}}).values():
            flow.setdefault("scopes", {{}})
    return spec


def entry(route) -> dict:
    return OVERLAY.get(route.operation_id or "") or OVERLAY.get(f"{{route.method}} {{route.path}}") or {{}}


def route_map(route, kind):
    found = entry(route)
    return MCPType.TOOL if found and not found.get("exclude") else MCPType.EXCLUDE


def component(route, tool) -> None:
    found = entry(route)
    if found.get("description"):
        tool.description = found["description"]
    tool.annotations = ToolAnnotations(
        readOnlyHint=bool(found.get("read_only")),
        destructiveHint=bool(found.get("destructive")),
        idempotentHint=bool(found.get("idempotent")),
    )


spec = load_spec()
client = httpx2.AsyncClient(
    base_url=os.environ.get("API_BASE_URL", {base_url!r}),
    headers=json.loads(os.environ.get("API_HEADERS", "{{}}")),
    timeout=30,
)
mcp = FastMCP.from_openapi(
    openapi_spec=spec,
    client=client,
    name={title!r},
    route_map_fn=route_map,
    mcp_component_fn=component,
    mcp_names={{k: v["tool"] for k, v in OVERLAY.items() if v.get("op_id")}},
)
mcp.add_middleware(CapResponseMiddleware())


class Merged(Tool):
    """One tool for several operations. The selector argument picks which one runs."""

    plan: dict

    async def run(self, arguments):
        plan = self.plan
        op = plan["operations"].get(str(arguments.get(plan["selector"])))
        if op is None:
            choices = ", ".join(plan["operations"])
            problem = {{"error": f"{{plan['selector']}} must be one of {{choices}}"}}
            return ToolResult(structured_content=problem, is_error=True)
        path, query, headers = op["path"], {{}}, {{}}
        for p in op["params"]:
            value = arguments.get(p["arg"])
            if value is None:
                continue
            if p["in"] == "path":
                path = path.replace("{{" + p["name"] + "}}", quote(str(value), safe=""))
            elif p["in"] == "query":
                query[p["name"]] = value
            elif p["in"] == "header":
                headers[p["name"]] = str(value)
        body = None
        if op["body"] == "fields":
            body = {{f["name"]: arguments[f["arg"]] for f in op["fields"] if arguments.get(f["arg"]) is not None}}
        elif op["body"] == "whole":
            body = arguments.get("body")
        reply = await client.request(op["method"], path, params=query, headers=headers, json=body)
        try:
            data = reply.json()
        except ValueError:
            data = reply.text
        shaped = data if isinstance(data, dict) else {{"result": data}}
        return ToolResult(structured_content=shaped, is_error=reply.status_code >= 400)


for name, plan in MERGED.items():
    found = OVERLAY.get(name) or {{}}
    if found.get("exclude"):
        continue
    mcp.add_tool(
        Merged(
            name=name,
            description=found.get("description") or plan["description"],
            parameters=plan["parameters"],
            annotations=ToolAnnotations(readOnlyHint=plan["read_only"], destructiveHint=plan["destructive"]),
            plan=plan,
        )
    )

if __name__ == "__main__":
    mcp.run(show_banner=False)
'''


def dump(path: Path, data: Any) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    return path


def key(entry: dict) -> str:
    return entry["op_id"] or entry["route"] or entry["tool"]


def target(route: str) -> str:
    method, _, path = route.partition(" ")
    return "$.paths['{}'].{}".format(path.replace("'", "\\'"), method.lower())


def overlay(entries: list[dict], spec: str | None) -> dict[str, Any]:
    ops = {}
    for e in entries:
        ops[key(e)] = {
            "tool": e["tool"],
            "op_id": e["op_id"],
            "route": e["route"],
            "description": e["description"],
            "original": e["original"],
            "status": e["status"],
            "beats_original": bool(e.get("beats_original")),
            "reason": e["reason"],
            "read_only": e["read_only"],
            "destructive": e["destructive"],
            "idempotent": bool(e.get("idempotent")),
            "exclude": bool(e.get("exclude")),
            **({"review": e["review_state"]} if e.get("review_state") else {}),
            **({"suggest_exclude": e["suggest_exclude"]} if e.get("suggest_exclude") else {}),
            **({"notes": e["notes"]} if e.get("notes") else {}),
        }
    return {"source": spec, "generated_by": f"misroute {version('misroute')}", "operations": ops}


def openapi_overlay(entries: list[dict], title: str) -> dict[str, Any]:
    """OpenAPI Overlay 1.0 actions that set each changed operation's description."""
    actions = [
        {"target": target(e["route"]), "update": {"description": e["description"]}}
        for e in entries
        if e["route"] and e["description"] != e["original"]
    ]
    return {
        "overlay": "1.0.0",
        "info": {"title": f"misroute descriptions for {title}", "version": "1"},
        "actions": actions,
    }


def apply(doc: dict, overlay_doc: dict) -> dict:
    """Apply update actions of an OpenAPI Overlay written by openapi_overlay to a spec, in place."""
    for action in overlay_doc.get("actions", []):
        path, _, method = action["target"][len("$.paths['") :].rpartition("'].")
        op = (doc.get("paths") or {}).get(path.replace("\\'", "'"), {}).get(method)
        if op is not None:
            op.update(action.get("update") or {})
    return doc


def server(out: Path, spec: str, title: str, base_url: str) -> str:
    full, here = Path(spec).resolve(), out.resolve()
    rel = os.path.relpath(full, here) if full.drive == here.drive else str(full)
    return SERVER.format(
        fastmcp=FASTMCP, title=title, misroute=version("misroute"), spec=Path(rel).as_posix(), base_url=base_url
    )


def write(
    out: Path,
    entries: list[dict],
    spec: str | None = None,
    doc: dict | None = None,
    fastmcp: bool = False,
    merged: dict | None = None,
) -> dict[str, str]:
    """Write descriptions.yaml always, overlay.yaml and overlay.openapi.yaml for an OpenAPI source, and server.py
    with fastmcp. When the source was converted from Swagger 2.0 or repaired, the result is written as
    openapi.json and the server uses that. merged holds what the server needs to run tools that merge several
    operations, written as merged.json and kept when a later write passes none. Nothing is applied to the
    source."""
    out.mkdir(parents=True, exist_ok=True)
    changed = {e["tool"]: e["description"] for e in entries if e["description"] != e["original"]}
    files = {"descriptions": str(dump(out / "descriptions.yaml", changed))}
    if spec and doc is not None:
        title = str((doc.get("info") or {}).get("title") or Path(spec).stem)
        files["overlay"] = str(dump(out / "overlay.yaml", overlay(entries, str(Path(spec).resolve()))))
        files["openapi_overlay"] = str(dump(out / "overlay.openapi.yaml", openapi_overlay(entries, title)))
        served = spec
        if merged:
            (out / "merged.json").write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
        if (out / "merged.json").exists():
            files["merged"] = str(out / "merged.json")
        if doc.get(FIXES):
            fixed = out / "openapi.json"
            clean = {k: v for k, v in doc.items() if k != FIXES}
            fixed.write_text(json.dumps(clean, indent=1, ensure_ascii=False), encoding="utf-8")
            files["spec"], served = str(fixed), str(fixed)
        if fastmcp:
            base = next((s.get("url") for s in doc.get("servers") or [] if s.get("url")), "http://127.0.0.1:9")
            path = out / "server.py"
            path.write_text(server(out, served, title, base), encoding="utf-8")
            files["server"] = str(path)
    return files
