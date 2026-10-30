import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from fnmatch import fnmatch
from typing import Any
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from misroute.config import ServerConfig
from misroute.server.catalog import Tool, from_mcp


def headers(cfg: ServerConfig) -> dict[str, str]:
    h = dict(cfg.headers)
    if cfg.auth:
        kind, _, var = cfg.auth.partition(":")
        if kind != "bearer_env" or not var:
            raise ValueError("auth must look like bearer_env:VAR")
        token = os.environ.get(var)
        if not token:
            raise ValueError(f"env var {var} is not set")
        h["Authorization"] = f"Bearer {token}"
    return h


def target(cfg: ServerConfig) -> Any:
    if cfg.command:
        env = {**os.environ, **{k: os.path.expandvars(v) for k, v in cfg.env.items()}} if cfg.env else None
        return StdioServerParameters(command=cfg.command[0], args=cfg.command[1:], env=env)
    h = headers(cfg)
    assert cfg.url
    if h:
        return streamable_http_client(cfg.url, http_client=create_mcp_http_client(headers=h))
    return cfg.url


@asynccontextmanager
async def connect(server: ServerConfig | Any) -> AsyncGenerator[Client]:
    src = target(server) if isinstance(server, ServerConfig) else server
    async with Client(src) as client:
        yield client


def keep(name: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(fnmatch(name, p) for p in include):
        return False
    return not any(fnmatch(name, p) for p in exclude)


async def list_tools(client: Client, include: list[str] | None = None, exclude: list[str] | None = None) -> list[Tool]:
    tools, cursor = [], None
    while True:
        page = await client.list_tools(cursor=cursor)
        tools += [from_mcp(t) for t in page.tools if keep(t.name, include or [], exclude or [])]
        cursor = page.next_cursor
        if not cursor:
            return tools
