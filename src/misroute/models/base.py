import json
from contextvars import ContextVar
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
    reasoning_tokens: int = 0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "Turn":
        calls = [Call(**c) for c in d["calls"]]
        made = cls(calls, d["text"], d["tokens_in"], d["tokens_out"], d.get("raw", ""), d.get("cost"))
        made.reasoning_tokens = d.get("reasoning_tokens", 0)
        return made


class Adapter(Protocol):
    name: str
    family: str

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None) -> Turn: ...

    async def complete(self, prompt: str, kind: str | None = None) -> str: ...

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None: ...


class BudgetExceeded(RuntimeError):
    pass


class ProviderRefused(BudgetExceeded):
    pass


class Budget:
    def __init__(self, limit_usd: float):
        self.limit = limit_usd
        self.spent = 0.0
        self.reserved = 0.0

    def reserve(self, need: float, share: int = 1) -> float:
        free = self.limit - self.spent - self.reserved
        if need > free:
            raise BudgetExceeded(f"budget {self.limit:.2f} usd reached, {self.spent:.2f} spent")
        held = max(need, free / max(share, 1))
        self.reserved += held
        return held

    def settle(self, held: float, cost: float = 0.0) -> None:
        self.reserved = max(0.0, self.reserved - held)
        self.spent += cost


def estimate_tokens(*parts: Any) -> int:
    return sum(len(p if isinstance(p, str) else json.dumps(p)) for p in parts) // 4 + 1


OUTPUT_ROOM: ContextVar[int | None] = ContextVar("output_room", default=None)
NO_LIMIT_ABOVE = 32768


def affordable(wanted: int | None, used: int = 0) -> int | None:
    room = OUTPUT_ROOM.get()
    if room is None:
        return wanted
    left = max(room - used, 0)
    if wanted is None and left >= NO_LIMIT_ABOVE:
        return None
    return min(wanted, left) if wanted else left


def merged(earlier: Turn, later: Turn) -> Turn:
    later.tokens_in += earlier.tokens_in
    later.tokens_out += earlier.tokens_out
    later.reasoning_tokens += earlier.reasoning_tokens
    if earlier.cost is not None or later.cost is not None:
        later.cost = (earlier.cost or 0.0) + (later.cost or 0.0)
    return later


async def until_whole(call, first: int | None) -> "Turn":
    """Call with a token limit, and while the reply is cut off at the limit, again with twice the tokens it used,
    until it is whole, the provider refuses a larger limit or the budget has no room for one. Every attempt is
    billed, so the returned turn carries the tokens of all of them."""
    turn = await call(affordable(first))
    last = turn.tokens_out
    while turn.truncated:
        limit = affordable(max(2 * last, 2 * (first or 0), 1), turn.tokens_out)
        if limit is not None and limit <= last:
            return turn
        try:
            nxt = await call(limit)
        except Exception:
            return turn
        if nxt.tokens_out <= last and nxt.truncated:
            return merged(turn, nxt)
        first = max(2 * last, first or 0)
        last = nxt.tokens_out
        turn = merged(turn, nxt)
    return turn


def parse_json(text: str) -> Any:
    start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
    if start < 0:
        raise ValueError("no json in reply")
    closing = "}" if text[start] == "{" else "]"
    end = text.rfind(closing)
    return json.loads(text[start : end + 1])
