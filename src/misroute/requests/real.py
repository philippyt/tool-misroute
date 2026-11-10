import json
from collections import Counter
from pathlib import Path
from typing import Any
from misroute.server.catalog import Tool

TEXT_KEYS = ("text", "request", "input", "prompt", "user")
CALL_KEYS = ("calls", "tool_calls", "tools")


def read(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def write(path: str | Path, rows: list[dict[str, Any]]) -> None:
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    Path(path).write_text(text, encoding="utf-8")


def call_name(call: Any) -> str:
    if isinstance(call, dict):
        inner = call.get("function") if isinstance(call.get("function"), dict) else call
        return str(inner.get("name") or inner.get("tool") or "")
    return str(call)


def turns(rows: list[dict[str, Any]]) -> list[tuple[str, list[str]]]:
    """Each user request with the tools called for it. Reads a guard log, where a user line is followed by its
    call lines, and trace lines that hold a request together with its calls or its tool."""
    out: list[tuple[str, list[str]]] = []
    for row in rows:
        kind = row.get("type")
        if kind == "user" and row.get("text"):
            out.append((str(row["text"]), []))
        elif kind == "call" and row.get("tool"):
            if out:
                out[-1][1].append(str(row["tool"]))
        elif kind is None or kind not in ("event", "held"):
            text = next((row[k] for k in TEXT_KEYS if isinstance(row.get(k), str) and row[k].strip()), None)
            calls = next((row[k] for k in CALL_KEYS if isinstance(row.get(k), list)), None)
            names = (
                [call_name(c) for c in calls] if calls is not None else [str(row["tool"])] if row.get("tool") else []
            )
            if text:
                out.append((text, [n for n in names if n]))
    return out


def proposed(calls: list[str], by: dict[str, Tool]) -> str | None:
    """The tool a request most likely wanted: its first write, else its first call. A suggestion for a person
    to confirm, since what an agent called is not proof of what was right."""
    known = [c for c in calls if c in by]
    return next((c for c in known if by[c].is_write), known[0] if known else None)


def collect(source: str | Path, tools: list[Tool], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """New unreviewed candidates from a log, skipping requests already in the file and requests where no known
    tool was called."""
    by = {t.name: t for t in tools}
    seen = {r.get("text") for r in existing}
    out = []
    for text, calls in turns(read(source)):
        tool = proposed(calls, by)
        if tool and text not in seen:
            seen.add(text)
            out.append({"text": text, "tool": tool, "called": calls, "reviewed": False})
    return out


def status(rows: list[dict[str, Any]], tools: list[Tool]) -> dict[str, Any]:
    names = {k: t.name for t in tools for k in (t.name, t.op_id, t.route) if k}
    done = Counter(names.get(str(r.get("tool")), "") for r in rows if r.get("reviewed", True))
    done.pop("", None)
    return {
        "requests": len(rows),
        "reviewed": sum(bool(r.get("reviewed", True)) for r in rows),
        "pending": sum(not r.get("reviewed", True) for r in rows),
        "per_tool": dict(sorted(done.items())),
        "tools_without": sorted(t.name for t in tools if not done.get(t.name)),
    }
