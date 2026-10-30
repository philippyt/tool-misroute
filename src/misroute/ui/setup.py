import os
import shlex
from pathlib import Path
from typing import Any
import yaml
from misroute.config import Config, env_path
from misroute.models import build
from misroute.project import Project

TOKEN_VAR = "MISROUTE_MCP_TOKEN"
KEY_VAR = "MISROUTE_MODEL_KEY"


def server_block(form: dict) -> dict[str, Any]:
    kind, value = form.get("server_kind", "url"), str(form.get("server_value", "")).strip()
    if not value:
        raise ValueError("server is empty")
    out: dict[str, Any] = {"command": shlex.split(value, posix=os.name != "nt")} if kind == "command" else {kind: value}
    if form.get("server_token") or (form.get("keep_token") and os.environ.get(TOKEN_VAR)):
        out["auth"] = f"bearer_env:{TOKEN_VAR}"
    include = [x.strip() for x in str(form.get("include_tools", "")).split(",") if x.strip()]
    if include:
        out["include_tools"] = include
    return out


def model_block(form: dict) -> dict[str, Any]:
    provider = form.get("provider", "openai-compatible")
    out: dict[str, Any] = {"name": form.get("name") or "main", "provider": provider, "model": form.get("model")}
    if form.get("base_url"):
        out["base_url"] = form["base_url"]
    if provider != "bedrock":
        out["api_key_env"] = KEY_VAR
    for k in ("price_in", "price_out"):
        if form.get(k) not in (None, ""):
            out[k] = float(form[k])
    options: dict[str, Any] = {}
    if provider == "azure-openai" and form.get("api_version"):
        options["api_version"] = form["api_version"]
    if form.get("no_temperature"):
        options["temperature"] = None
    if options:
        out["options"] = options
    return out


def merged(config: Path, form: dict) -> dict[str, Any]:
    data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.exists() else {}
    data = data or {}
    model = model_block(form)
    others = [m for m in data.get("models", []) if m.get("name") != model["name"]]
    data.update(server=server_block(form), models=[model, *others])
    names = {m["name"] for m in data["models"]}
    for role in ("generator", "validator", "writer"):
        if data.get(role) not in names:
            data.pop(role, None)
    data["modes"] = form.get("modes") or data.get("modes") or ["json"]
    if form.get("budget_usd") not in (None, ""):
        data["budget_usd"] = float(form["budget_usd"])
    return data


def secrets(form: dict) -> dict[str, str]:
    out = {}
    if form.get("server_token"):
        out[TOKEN_VAR] = form["server_token"]
    if form.get("api_key"):
        out[KEY_VAR] = form["api_key"]
    return out


def save(config: Path, form: dict) -> dict[str, Any]:
    data = merged(config, form)
    Config.model_validate(data)
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    new = secrets(form)
    if new:
        path = env_path(config)
        lines = [ln for ln in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])]
        lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in new]
        path.write_text("\n".join([*lines, *(f"{k}={v}" for k, v in new.items())]) + "\n", encoding="utf-8")
        os.environ.update(new)
    return {"saved": str(config), "env": sorted(new)}


async def test(config: Path, form: dict) -> dict[str, Any]:
    """List the server's tools, which is free, and make one tiny model call to check the key."""
    os.environ.update(secrets(form))
    out: dict[str, Any] = {}
    try:
        cfg = Config.model_validate(merged(config, form))
    except Exception as e:
        return {"error": f"config: {e}"}
    try:
        tools = await Project(cfg, root=config.parent / ".misroute").tools()
        out["tools"] = {"count": len(tools), "names": [t.name for t in tools[:15]]}
    except Exception as e:
        out["tools"] = {"error": f"{type(e).__name__}: {e}"[:600]}
    try:
        turn = await build(cfg.models[0]).chat("Reply with the single word ok.", [])
        out["model"] = {"reply": turn.text[:80], "tokens_in": turn.tokens_in, "tokens_out": turn.tokens_out}
    except Exception as e:
        out["model"] = {"error": f"{type(e).__name__}: {e}"[:600]}
    return out


def current(config: Path) -> dict[str, Any]:
    if not config.exists():
        return {"configured": False}
    data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    server, model = data.get("server", {}), (data.get("models") or [{}])[0]
    kind = next((k for k in ("url", "command", "openapi") if server.get(k)), "url")
    value = " ".join(server["command"]) if kind == "command" else server.get(kind, "")
    return {
        "configured": True,
        "server_kind": kind,
        "server_value": value,
        "include_tools": ", ".join(server.get("include_tools", [])),
        "has_token": bool(os.environ.get(TOKEN_VAR)),
        "has_key": bool(os.environ.get(model.get("api_key_env") or KEY_VAR)),
        "provider": model.get("provider", "openai-compatible"),
        "name": model.get("name", "main"),
        "model": model.get("model", ""),
        "base_url": model.get("base_url", ""),
        "price_in": model.get("price_in", ""),
        "price_out": model.get("price_out", ""),
        "api_version": (model.get("options") or {}).get("api_version", ""),
        "budget_usd": data.get("budget_usd", 5),
        "modes": data.get("modes", ["json"]),
    }
