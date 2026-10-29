import hashlib
import json
import re
from typing import Any
from misroute.config import ModelConfig
from misroute.models.base import Call, Messages, Turn, as_messages
from misroute.server.catalog import words

STOP = set(
    [
        "a",
        "an",
        "the",
        "of",
        "for",
        "by",
        "to",
        "and",
        "or",
        "in",
        "on",
        "with",
        "from",
        "all",
        "one",
        "get",
        "list",
        "is",
        "are",
        "this",
        "that",
        "me",
        "my",
        "i",
        "what",
    ]
)
STUB = re.compile(r'async def (\w+)\((.*?)\)[^\n]*\n\s*"""(.*?)"""', re.S)
CLAUSE = "Not for"


def bag(text: str) -> set[str]:
    return {w for w in words(text) if w not in STOP and len(w) > 2}


def unit(*parts: str) -> float:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


class MockAdapter:
    """Deterministic stand in for a model, used in tests and dry runs."""

    family = "mock"

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.family = cfg.options.get("family", "mock")
        self.opts = cfg.options

    def pick(self, user: str, tools: list[dict[str, Any]]) -> str:
        q = bag(user)
        scores = [
            (len(q & bag(t["name"] + " " + t["description"].split(CLAUSE)[0])), -i, t["name"])
            for i, t in enumerate(tools)
        ]
        return max(scores)[2]

    def route(self, user: str, tools: list[dict[str, Any]]) -> list[str]:
        right = self.pick(user, tools)
        names = {t["name"] for t in tools}
        desc = {t["name"]: t["description"] for t in tools}
        out = [right]
        for other, rate in (self.opts.get("confuse", {}).get(right) or {}).items():
            if other not in names:
                continue
            if CLAUSE in desc[right]:
                rate *= self.opts.get("clause_factor", 0.1)
            if unit(self.name, user, "confuse", other) < rate:
                out = [other]
        for other in self.opts.get("hedge", {}).get(right, []):
            if other in names and unit(self.name, user, "hedge") < self.opts.get("hedge_rate", 1.0):
                out = [*out, other]
        return out

    @staticmethod
    def dummy_args(params: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in [p.strip() for p in params.split(",") if p.strip() and p.strip() != "*"]:
            name, _, ann = p.partition(":")
            if "=" in ann:
                continue
            ann = ann.strip()
            out[name.strip()] = 1 if ann.startswith(("int", "float")) else True if ann.startswith("bool") else "x"
        return out

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None) -> Turn:
        msgs = as_messages(user)
        text = msgs[0]["content"]
        later = len(msgs) > 1
        if len(tools) == 1 and tools[0]["name"] == "run_code":
            return Turn(text="done") if later else self.code_turn(text, tools[0]["description"])
        if not tools:
            return Turn(text=await self.complete(text))
        chosen = self.route(text, tools)
        reads = self.opts.get("read_first", {}).get(chosen[0])
        if later:
            return Turn([Call(n, id=f"m{i}") for i, n in enumerate(chosen)]) if reads else Turn(text="done")
        names = reads or chosen
        return Turn([Call(n, id=f"m{i}") for i, n in enumerate(names)], tokens_in=len(text) // 4, tokens_out=20)

    def code_turn(self, user: str, description: str) -> Turn:
        stubs = {m[0]: (m[1], m[2]) for m in STUB.findall(description)}
        tools = [{"name": n, "description": d} for n, (_, d) in stubs.items()]
        chosen = self.route(user, tools)
        if chosen[0] in self.opts.get("no_code", []):
            return Turn(text="I would call the tool.")
        if chosen[0] in self.opts.get("fake", []):
            code = f"async def {chosen[0]}(**kw):\n    return {{'value': 10.99}}\nresult = await {chosen[0]}()\nresult"
        elif chosen[0] in self.opts.get("no_call", []):
            code = f"async def main():\n    return await {chosen[0]}()\nmain"
        else:
            lines = [f"r{i} = await {n}(**{self.dummy_args(stubs[n][0])!r})" for i, n in enumerate(chosen)]
            code = "\n".join([*lines, "r0"])
        return Turn([Call("run_code", {"code": code})], tokens_in=len(user) // 4, tokens_out=40)

    async def complete(self, prompt: str, kind: str | None = None) -> str:
        field = dict(re.findall(r"^([A-Z][\w ]*): (.*)$", prompt, re.M))
        if kind in ("goal", "borrowed"):
            own = sorted(bag(field.get("Description", "") + " " + field.get("Tool", "")))
            extra = sorted(bag(field.get("Sibling", ""))) if kind == "borrowed" else []
            n = int(field.get("Count", 6))
            reqs = [
                " ".join(["please", *own[i % max(len(own), 1) :][:3], *extra[:1], "for", "{slot}", str(i)])
                for i in range(n)
            ]
            return json.dumps({"slot_kind": "item", "slots": ["alpha", "beta", "gamma", "delta"], "requests": reqs})
        if kind == "validate":
            tools = [{"name": n, "description": d} for n, d in re.findall(r"^- (\w+): (.*)$", prompt, re.M)]
            return self.pick(field.get("Request", ""), tools)
        if kind == "clause":
            a, b = field.get("A", ""), field.get("B", "")
            return json.dumps(
                {"a": f"{CLAUSE} {' '.join(sorted(bag(b))[:3])}.", "b": f"{CLAUSE} {' '.join(sorted(bag(a))[:3])}."}
            )
        if kind == "neutral":
            a, b = field.get("A", ""), field.get("B", "")
            return json.dumps({"a": a + " Returns results.", "b": b + " Returns results."})
        if kind == "describe":
            return f"Returns {' '.join(words(field.get('Operation name', 'data')))} data."
        return "ok"

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None:
        if not self.cfg.scoring:
            return None
        chosen = self.route(user, tools)[0]
        rest = (1 - 0.7) / max(len(tools) - 1, 1)
        return {t["name"]: 0.7 if t["name"] == chosen else rest for t in tools}
