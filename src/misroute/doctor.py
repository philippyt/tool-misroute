import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any
from misroute.config import Config, env_path, load
from misroute.models import build
from misroute.store.cache import fill_price


def hint(err: str) -> str:
    e = err.lower()
    if "certificate" in e or "ssl" in e:
        return "TLS failed, likely a company proxy. Set system_certs: true in misroute.yaml or MISROUTE_SYSTEM_CERTS=1"
    if "401" in e or "403" in e or "unauthorized" in e or "authentication" in e or "api key" in e:
        return "the key or token was refused, check the env var and that it belongs to this endpoint"
    if "404" in e or "not found" in e or "deployment" in e:
        return "the endpoint answered but does not know the model, check the model or deployment name and base_url"
    if "getaddrinfo" in e or "name or service" in e or "nodename" in e or "dns" in e:
        return "the host name did not resolve, check the URL or whether it is on a private network"
    if "connect" in e or "timed out" in e or "refused" in e:
        return "could not connect, check the URL, the port, VPN or that the local server is running"
    if "402" in e or "credit" in e or "quota" in e:
        return "the provider refused for billing reasons, top up credits or raise the quota"
    return ""


def row(check: str, ok: bool, detail: str = "", fix: str = "") -> dict[str, Any]:
    return {"check": check, "ok": ok, "detail": detail, "fix": fix}


async def run(config: Path, offline: bool = False) -> list[dict[str, Any]]:
    rows = [
        row("python", sys.version_info >= (3, 11), sys.version.split()[0], "misroute needs Python 3.11 or newer"),
        row("misroute", True, version("misroute")),
    ]
    if not config.exists():
        rows.append(row("config", False, f"{config} not found", "run misroute init, or misroute quick <server>"))
        return rows
    try:
        cfg: Config = load(config)
    except Exception as e:
        rows.append(row("config", False, str(e)[:300], "fix the field named in the error"))
        return rows
    rows.append(row("config", True, str(config)))
    env = env_path(config)
    rows.append(row(".env", True, f"loaded from {env}" if env.exists() else "none, keys come from the environment"))
    from misroute.project import Project

    try:
        tools = await Project(cfg, root=config.parent / cfg.workdir).tools()
        rows.append(row("server", bool(tools), f"{len(tools)} tools", "" if tools else "the server listed no tools"))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        rows.append(row("server", False, err[:300], hint(err)))
    for m in cfg.models:
        key_ok = not m.api_key_env or bool(os.environ.get(m.api_key_env))
        rows.append(row(f"{m.name} key", key_ok, m.api_key_env or "no key needed", f"set {m.api_key_env}"))
        priced = fill_price(m)
        detail = f"${m.price_in} in, ${m.price_out} out per million tokens" if priced else "unknown"
        rows.append(row(f"{m.name} price", priced, detail, "set price_in and price_out, or estimates show $0"))
        if offline or not key_ok:
            continue
        try:
            turn = await build(m).chat("Reply with the single word ok.", [])
            rows.append(row(f"{m.name} call", True, f"replied {turn.text.strip()[:20]!r}"))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            rows.append(row(f"{m.name} call", False, err[:300], hint(err)))
    return rows


def system_certs(cfg_path: Path | None = None) -> None:
    """Trust the operating system certificate store, which corporate TLS proxies usually install into."""
    wanted = os.environ.get("MISROUTE_SYSTEM_CERTS") in ("1", "true")
    if not wanted and cfg_path and cfg_path.exists():
        try:
            wanted = bool(load(cfg_path).system_certs)
        except Exception:
            wanted = False
    if wanted:
        import truststore

        truststore.inject_into_ssl()
