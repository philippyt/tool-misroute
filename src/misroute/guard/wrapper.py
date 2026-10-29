import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from misroute.guard.rules import Event, Rules

CallFn = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass
class Held:
    id: str
    tool: str
    other: str | None
    message: str


def call_id(name: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([name, args], sort_keys=True, default=str).encode()).hexdigest()[:12]


class Guard:
    """Logs every real tool call and applies sibling rules. Works for JSON calls and code mode functions."""

    def __init__(self, call: CallFn, rules: Rules, log: str | Path, otel: bool = False):
        self.call = call
        self.rules = rules
        self.log_path = Path(log)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.approved_path = self.log_path.with_name("approved.json")
        self.history: list[tuple[str, float]] = []
        self.events: list[Event] = []
        self.last_user: tuple[str, float] | None = None
        self.tracer = None
        if otel:
            from opentelemetry import trace

            self.tracer = trace.get_tracer("misroute.guard")

    def _log(self, entry: str, **data: Any) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.time(), "type": entry, **data}, default=str) + "\n")

    def approved(self) -> set[str]:
        return set(json.loads(self.approved_path.read_text())) if self.approved_path.exists() else set()

    def user_turn(self, text: str) -> Event | None:
        now = time.time()
        ev = self.rules.rephrase(text, self.last_user[0], now - self.last_user[1]) if self.last_user else None
        self.last_user = (text, now)
        if ev:
            self.events.append(ev)
            self._log("event", **asdict(ev))
        return ev

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        now = time.time()
        cid = call_id(name, args)
        events = self.rules.check(name, self.history, now)
        for ev in events:
            self.events.append(ev)
            self._log("event", id=cid, **asdict(ev))
        hold = next((e for e in events if e.kind == "hold"), None)
        if hold and cid not in self.approved():
            msg = f"held for confirmation: {name} after {hold.other}, approve with misroute guard confirm {cid}"
            self._log("held", id=cid, tool=name, args=args, other=hold.other)
            return Held(cid, name, hold.other, msg)
        self.history.append((name, now))
        self._log("call", id=cid, tool=name, args=args, flags=[e.kind for e in events])
        if self.tracer:
            with self.tracer.start_as_current_span(f"tool {name}") as span:
                span.set_attribute("misroute.tool", name)
                span.set_attribute("misroute.flags", ",".join(e.kind for e in events))
                return await self.call(name, args)
        return await self.call(name, args)

    def functions(self, names: list[str]) -> dict[str, Callable[..., Awaitable[Any]]]:
        def make(name: str):
            async def fn(**kwargs: Any) -> Any:
                out = await self.call_tool(name, kwargs)
                if isinstance(out, Held):
                    raise PermissionError(out.message)
                return out

            return fn

        return {n: make(n) for n in names}

    def program(self, code: str, calls_before: int, tools: list[str]) -> Event | None:
        from misroute.detectors.codemode import defined

        made = len(self.history) - calls_before
        ev = None
        if defined(code) & set(tools):
            ev = Event("faked", ",".join(sorted(defined(code) & set(tools))))
        elif made == 0:
            ev = Event("no_call", "")
        if ev:
            self.events.append(ev)
            self._log("event", **asdict(ev))
        return ev


def approve(log: str | Path, cid: str) -> None:
    path = Path(log).with_name("approved.json")
    ids = set(json.loads(path.read_text())) if path.exists() else set()
    ids.add(cid)
    path.write_text(json.dumps(sorted(ids)))


def wrap(client: Any, rules: Rules, log: str | Path, otel: bool = False) -> Guard:
    async def call(name: str, args: dict[str, Any]) -> Any:
        return await client.call_tool(name, args)

    return Guard(call, rules, log, otel)
