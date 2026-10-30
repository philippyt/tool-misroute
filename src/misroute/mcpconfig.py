import json
import os
import sys
from pathlib import Path
from typing import Any

TOKEN_VAR = "MISROUTE_MCP_TOKEN"


def locations() -> dict[str, list[Path]]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    claude_desktop = {
        "win32": appdata / "Claude" / "claude_desktop_config.json",
        "darwin": home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    }.get(sys.platform, home / ".config" / "Claude" / "claude_desktop_config.json")
    return {
        "claude": [Path(".mcp.json"), home / ".claude.json", claude_desktop],
        "cursor": [Path(".cursor") / "mcp.json", home / ".cursor" / "mcp.json"],
        "vscode": [Path(".vscode") / "mcp.json"],
    }


def servers_in(path: Path) -> dict[str, dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("mcpServers") or data.get("servers") or {})


def found(source: str | None = None) -> list[tuple[str, str, Path, dict[str, Any]]]:
    """MCP servers from Claude, Cursor and VS Code configs, or from one JSON file passed as source."""
    if source and source not in locations():
        path = Path(source)
        return [("file", name, path, spec) for name, spec in servers_in(path).items()]
    out = []
    for client, paths in locations().items():
        if source and client != source:
            continue
        for path in paths:
            if path.exists():
                try:
                    out += [(client, name, path, spec) for name, spec in servers_in(path).items()]
                except (ValueError, OSError):
                    continue
    return out


def to_server(spec: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """A server block for misroute.yaml and secrets for .env, so tokens never land in the YAML."""
    secrets: dict[str, str] = {}
    if spec.get("command"):
        out: dict[str, Any] = {"command": [spec["command"], *spec.get("args", [])]}
        if spec.get("env"):
            out["env"] = {k: f"${{{k}}}" for k in spec["env"]}
            secrets.update({k: str(v) for k, v in spec["env"].items()})
        return out, secrets
    url = spec.get("url") or spec.get("serverUrl")
    if not url:
        raise ValueError("server entry has neither command nor url")
    out = {"url": url}
    headers = dict(spec.get("headers") or {})
    auth = headers.pop("Authorization", None) or headers.pop("authorization", None)
    if auth and auth.lower().startswith("bearer "):
        secrets[TOKEN_VAR] = auth.split(" ", 1)[1]
        out["auth"] = f"bearer_env:{TOKEN_VAR}"
    if headers:
        out["headers"] = headers
    return out, secrets
