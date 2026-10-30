import os
from importlib.metadata import version
from pathlib import Path
from typing import Any
import yaml

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
from pathlib import Path
import httpx2
import yaml
from fastmcp import FastMCP
from fastmcp.server.providers.openapi import MCPType
from mcp_types import ToolAnnotations

HERE = Path(__file__).resolve().parent
SPEC = Path(os.environ.get("API_SPEC", HERE / {spec!r}))
OVERLAY = yaml.safe_load((HERE / "overlay.yaml").read_text(encoding="utf-8"))["operations"]


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
        readOnlyHint=bool(found.get("read_only")), destructiveHint=bool(found.get("destructive"))
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
            "reason": e["reason"],
            "read_only": e["read_only"],
            "destructive": e["destructive"],
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
    out: Path, entries: list[dict], spec: str | None = None, doc: dict | None = None, fastmcp: bool = False
) -> dict[str, str]:
    """Write descriptions.yaml always, overlay.yaml and overlay.openapi.yaml for an OpenAPI source, and server.py
    with fastmcp. Nothing is applied to the source."""
    out.mkdir(parents=True, exist_ok=True)
    changed = {e["tool"]: e["description"] for e in entries if e["description"] != e["original"]}
    files = {"descriptions": str(dump(out / "descriptions.yaml", changed))}
    if spec and doc is not None:
        title = str((doc.get("info") or {}).get("title") or Path(spec).stem)
        files["overlay"] = str(dump(out / "overlay.yaml", overlay(entries, str(Path(spec).resolve()))))
        files["openapi_overlay"] = str(dump(out / "overlay.openapi.yaml", openapi_overlay(entries, title)))
        if fastmcp:
            base = next((s.get("url") for s in doc.get("servers") or [] if s.get("url")), "http://127.0.0.1:9")
            path = out / "server.py"
            path.write_text(server(out, spec, title, base), encoding="utf-8")
            files["server"] = str(path)
    return files
