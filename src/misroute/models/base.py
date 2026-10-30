import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class Call:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    id: str = ""


Messages = str | list[dict[str, Any]]
"""A user prompt, or a conversation in neutral form: user content, assistant text with calls, tool results."""


def as_messages(user: Messages) -> list[dict[str, Any]]:
    return [{"role": "user", "content": user}] if isinstance(user, str) else user


@dataclass
class Turn:
    calls: list[Call] = field(default_factory=list)
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    raw: str = ""
    cost: float | None = None
    truncated: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Turn":
        calls = [Call(**c) for c in d["calls"]]
        return cls(calls, d["text"], d["tokens_in"], d["tokens_out"], d.get("raw", ""), d.get("cost"))


class Adapter(Protocol):
    name: str
    family: str

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None) -> Turn: ...

    async def complete(self, prompt: str, kind: str | None = None) -> str: ...

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None: ...


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, limit_usd: float):
        self.limit = limit_usd
        self.spent = 0.0
        self.lock = asyncio.Lock()

    def charge(self, cost: float) -> None:
        self.spent += cost

    def check(self, reserve: float = 0.0) -> None:
        if self.spent + reserve > self.limit:
            raise BudgetExceeded(f"budget {self.limit:.2f} usd reached")


def estimate_tokens(*parts: Any) -> int:
    return sum(len(p if isinstance(p, str) else json.dumps(p)) for p in parts) // 4 + 1


COMPLETE_TOKENS = 4096


def parse_json(text: str) -> Any:
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no json in reply")
    closing = "}" if text[start] == "{" else "]"
    end = text.rfind(closing)
    return json.loads(text[start : end + 1])
