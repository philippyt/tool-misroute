import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

WRITE_VERBS = set(
    [
        "create",
        "update",
        "delete",
        "remove",
        "add",
        "set",
        "post",
        "put",
        "patch",
        "write",
        "send",
        "insert",
        "upsert",
        "merge",
        "close",
        "cancel",
        "assign",
        "edit",
        "move",
        "archive",
        "approve",
        "submit",
        "reset",
        "upload",
        "replace",
        "rename",
        "transfer",
        "revoke",
        "grant",
        "enable",
        "disable",
        "start",
        "stop",
        "run",
    ]
)


def words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    read_only: bool | None = None
    destructive: bool | None = None
    route: str = ""
    op_id: str = ""

    @property
    def is_write(self) -> bool:
        if self.read_only is True:
            return False
        if self.destructive is True or self.read_only is False:
            return True
        return any(w in WRITE_VERBS for w in words(self.name)[:2])

    @property
    def params(self) -> dict[str, Any]:
        return self.input_schema.get("properties") or {}

    @property
    def required(self) -> list[str]:
        return list(self.input_schema.get("required") or [])

    def with_description(self, description: str) -> "Tool":
        return replace(self, description=description)

    def spec(self) -> dict[str, Any]:
        schema = self.input_schema or {"type": "object", "properties": {}}
        return {"name": self.name, "description": self.description, "input_schema": schema}

    @property
    def hash(self) -> str:
        blob = json.dumps(
            [self.name, self.description, self.input_schema, self.output_schema, self.read_only, self.destructive],
            sort_keys=True,
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def from_mcp(t: Any) -> Tool:
    ann = t.annotations
    return Tool(
        name=t.name,
        description=t.description or "",
        input_schema=dict(t.input_schema or {}),
        output_schema=dict(t.output_schema) if t.output_schema else None,
        read_only=ann.read_only_hint if ann else None,
        destructive=ann.destructive_hint if ann else None,
    )


def catalog_hash(tools: list[Tool]) -> str:
    return hashlib.sha256("".join(sorted(t.hash for t in tools)).encode()).hexdigest()[:16]


def replace_descriptions(tools: list[Tool], new: dict[str, str]) -> list[Tool]:
    return [t.with_description(new[t.name]) if t.name in new else t for t in tools]
