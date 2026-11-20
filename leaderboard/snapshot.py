#!/usr/bin/env python3
"""Snapshot tools/list from verified MCP servers in leaderboard/catalog.json.

Usage (from repo root):
    uv run python leaderboard/snapshot.py
    uv run python leaderboard/snapshot.py --only fetch,time,filesystem
    uv run python leaderboard/snapshot.py --skip-docker --timeout 90
    uv run python leaderboard/snapshot.py --rebuild-index
"""

from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import PaginatedRequestParams
from misroute.server.static import tools as load_static_tools

LEADERBOARD_DIR = Path(__file__).resolve().parent
CATALOG_PATH = LEADERBOARD_DIR / "catalog.json"
SERVERS_DIR = LEADERBOARD_DIR / "servers"
INDEX_PATH = SERVERS_DIR / "INDEX.json"
SERVERS_MD_PATH = LEADERBOARD_DIR / "SERVERS.md"


def resolve_command(cmd: str) -> str:
    if os.name == "nt":
        for candidate in (cmd, f"{cmd}.cmd", f"{cmd}.exe", f"{cmd}.bat"):
            found = shutil.which(candidate)
            if found:
                return found
    return shutil.which(cmd) or cmd


def lookup_registry_version(ecosystem: str, package: str) -> str | None:
    try:
        if ecosystem == "npm":
            npm = resolve_command("npm")
            res = subprocess.run(
                [npm, "view", package, "version"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            ver = res.stdout.strip().splitlines()[-1].strip() if res.stdout.strip() else ""
            return ver or None
        if ecosystem == "pypi":
            url = f"https://pypi.org/pypi/{package}/json"
            req = urllib.request.Request(url, headers={"User-Agent": "misroute-leaderboard/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return str(data.get("info", {}).get("version") or "") or None
        if ecosystem == "docker":
            docker = resolve_command("docker")
            res = subprocess.run(
                [docker, "inspect", "--format", "{{index .RepoDigests 0}}", package],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            digest = res.stdout.strip()
            return digest or None
    except Exception:
        return None
    return None


def serialize_tool(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    row: dict[str, Any] = {
        "name": str(tool.name),
        "description": str(getattr(tool, "description", None) or ""),
        "inputSchema": schema,
    }
    title = getattr(tool, "title", None)
    if title:
        row["title"] = str(title)
    output_schema = getattr(tool, "outputSchema", None)
    if isinstance(output_schema, dict):
        row["outputSchema"] = output_schema
    annotations = getattr(tool, "annotations", None)
    if annotations is not None:
        if hasattr(annotations, "model_dump"):
            ann = annotations.model_dump(exclude_none=True)
        elif isinstance(annotations, dict):
            ann = {k: v for k, v in annotations.items() if v is not None}
        else:
            ann = {}
        if ann:
            row["annotations"] = ann
    return row


async def fetch_tools_stdio(
    entry: dict[str, Any],
    timeout_s: float,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    cmd = resolve_command(entry["command"])
    raw_args = entry.get("args") or []
    args = [a.replace("{cwd}", str(LEADERBOARD_DIR)) for a in raw_args]

    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (entry.get("env") or {}).items()})

    params = StdioServerParameters(command=cmd, args=args, env=env)

    with tempfile.TemporaryFile(mode="w+b") as errlog:

        async def _run() -> tuple[list[dict[str, Any]], dict[str, str]]:
            async with stdio_client(params, errlog=errlog) as (read, write), ClientSession(read, write) as session:
                init_res = await session.initialize()
                info_obj = getattr(init_res, "serverInfo", None)
                server_info = {
                    "name": str(getattr(info_obj, "name", "") or ""),
                    "version": str(getattr(info_obj, "version", "") or ""),
                }
                tools_out: list[dict[str, Any]] = []
                cursor: str | None = None
                while True:
                    res = (
                        await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
                        if cursor
                        else await session.list_tools()
                    )
                    for t in res.tools:
                        tools_out.append(serialize_tool(t))
                    cursor = getattr(res, "nextCursor", None)
                    if not cursor:
                        break
                return tools_out, server_info

        try:
            return await asyncio.wait_for(_run(), timeout=timeout_s)
        except Exception as exc:
            errlog.seek(0)
            tail = errlog.read().decode("utf-8", errors="replace")[-600:].strip()
            msg = f"{type(exc).__name__}: {exc}"
            if tail:
                msg += f"\n--- stderr tail ---\n{tail}"
            raise RuntimeError(msg) from exc


async def capture_server(
    entry: dict[str, Any],
    timeout_s: float,
    force: bool,
) -> tuple[bool, str]:
    name = entry["name"]
    out_dir = SERVERS_DIR / name
    tools_path = out_dir / "tools.json"
    meta_path = out_dir / "meta.json"

    if not force and tools_path.exists() and meta_path.exists():
        try:
            parsed = load_static_tools(tools_path)
            return True, f"skipped (already captured: {len(parsed)} tools)"
        except Exception:
            pass

    tools_list, server_info = await fetch_tools_stdio(entry, timeout_s=timeout_s)
    if not tools_list:
        return False, "server returned 0 tools"

    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"tools": tools_list}, indent=2, ensure_ascii=False) + "\n"
    tools_bytes = payload.encode("utf-8")
    tools_path.write_bytes(tools_bytes)

    parsed = load_static_tools(tools_path)
    tool_count = len(parsed)
    write_count = sum(1 for t in parsed if t.is_write)
    has_output_schema_count = sum(1 for t in parsed if t.output_schema is not None)
    tools_sha256 = hashlib.sha256(tools_bytes).hexdigest()

    ecosystem = entry.get("ecosystem", "")
    reg_ver = lookup_registry_version(ecosystem, entry["package"])
    si_ver = server_info.get("version", "").strip()
    if reg_ver:
        source_ref = reg_ver
    elif si_ver and si_ver.lower() not in ("unknown", "0.0.0"):
        source_ref = si_ver
    else:
        source_ref = "unpinned"

    meta: dict[str, Any] = {
        "name": name,
        "display_name": entry["display_name"],
        "category": entry["category"],
        "repo_url": entry["repo_url"],
        "package": entry["package"],
        "source_ref": source_ref,
        "captured_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "capture_method": entry.get("capture_method", "mcp_stdio_tools_list"),
        "tool_count": tool_count,
        "write_count": write_count,
        "has_output_schema_count": has_output_schema_count,
        "tools_sha256": tools_sha256,
        "notes": entry.get("notes", ""),
    }
    if server_info.get("name") or server_info.get("version"):
        meta["server_info"] = server_info

    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return (
        True,
        f"captured {tool_count} tools ({write_count} writes, {has_output_schema_count} outputSchema, ref={source_ref})",
    )


def rebuild_index(catalog: dict[str, Any], failures: dict[str, str] | None = None) -> None:
    SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    metas: list[dict[str, Any]] = []
    for meta_path in sorted(SERVERS_DIR.glob("*/meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            tools_path = meta_path.parent / "tools.json"
            if tools_path.exists():
                load_static_tools(tools_path)
                metas.append(meta)
        except Exception as exc:
            print(f"[warn] skipping invalid {meta_path}: {exc}", file=sys.stderr)

    metas.sort(key=lambda m: (m.get("category", ""), m.get("name", "")))
    remote_only = catalog.get("remote_only_servers") or []

    index_obj = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "server_count": len(metas),
        "total_tools": sum(m.get("tool_count", 0) for m in metas),
        "total_writes": sum(m.get("write_count", 0) for m in metas),
        "total_with_output_schema": sum(m.get("has_output_schema_count", 0) for m in metas),
        "servers": metas,
        "remote_only_servers": remote_only,
    }
    INDEX_PATH.write_text(json.dumps(index_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    lines = [
        "# MCP Leaderboard Server Snapshots",
        "",
        f"Generated: `{index_obj['generated_at']}`  ",
        f"Captured servers: **{index_obj['server_count']}** | "
        f"Total tools: **{index_obj['total_tools']}** | "
        f"Write tools: **{index_obj['total_writes']}** | "
        f"With `outputSchema`: **{index_obj['total_with_output_schema']}**",
        "",
        "## Captured Servers",
        "",
        "| Name | Category | Tools | Writes | outputSchema | Version / Ref | Capture Method | Package |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for m in metas:
        lines.append(
            f"| [`{m['name']}`](servers/{m['name']}/tools.json) | {m['category']} | "
            f"{m['tool_count']} | {m['write_count']} | {m['has_output_schema_count']} | "
            f"`{m['source_ref']}` | `{m['capture_method']}` | `{m['package']}` |"
        )

    if failures:
        lines.extend(
            [
                "",
                "## Failed Local Captures (Last Run)",
                "",
                "| Name | Error |",
                "|---|---|",
            ]
        )
        for fname, err in sorted(failures.items()):
            short_err = err.splitlines()[0].replace("|", "\\|")[:140]
            lines.append(f"| `{fname}` | {short_err} |")

    if remote_only:
        lines.extend(
            [
                "",
                "## Closed-Source / OAuth-Only Official Remote Servers",
                "",
                "These official vendor MCP servers require interactive OAuth or live account credentials:",
                "",
                "| Name | Category | Endpoint | Auth | Notes |",
                "|---|---|---|---|---|",
            ]
        )
        for r in remote_only:
            lines.append(f"| `{r['name']}` | {r['category']} | `{r['endpoint']}` | {r['auth']} | {r['notes']} |")

    lines.append("")
    SERVERS_MD_PATH.write_text("\n".join(lines), encoding="utf-8")


async def main_async(args: argparse.Namespace) -> int:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if args.rebuild_index:
        rebuild_index(catalog)
        print(f"Rebuilt {INDEX_PATH} and {SERVERS_MD_PATH}")
        return 0

    only_set = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None
    servers = [s for s in catalog["servers"] if s.get("runnable", True)]
    if only_set is not None:
        servers = [s for s in servers if s["name"] in only_set]
    if args.skip_docker:
        servers = [s for s in servers if s.get("command") != "docker"]

    ok_count = 0
    failures: dict[str, str] = {}

    for idx, entry in enumerate(servers, 1):
        name = entry["name"]
        print(f"[{idx}/{len(servers)}] {name} ({entry['package']}) ...", flush=True)
        try:
            ok, msg = await capture_server(entry, timeout_s=args.timeout, force=args.force)
            if ok:
                ok_count += 1
                print(f"  OK: {msg}", flush=True)
            else:
                failures[name] = msg
                print(f"  FAIL: {msg}", flush=True)
        except Exception as exc:
            failures[name] = str(exc)
            first_line = str(exc).splitlines()[0]
            print(f"  FAIL: {first_line}", flush=True)

    rebuild_index(catalog, failures=failures)
    print(f"\nDone: {ok_count}/{len(servers)} captured, {len(failures)} failed.")
    print(f"Index: {INDEX_PATH}")
    print(f"Summary: {SERVERS_MD_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture MCP tools/list snapshots for the misroute leaderboard.")
    parser.add_argument("--only", help="Comma-separated server slugs to capture.")
    parser.add_argument("--skip-docker", action="store_true", help="Skip Docker-based servers.")
    parser.add_argument("--force", action="store_true", help="Re-capture servers that already have snapshots.")
    parser.add_argument("--timeout", type=float, default=90.0, help="Per-server timeout in seconds (default: 90).")
    parser.add_argument("--rebuild-index", action="store_true", help="Only rebuild INDEX.json and SERVERS.md.")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
