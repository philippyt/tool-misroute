import json
import math
import os
import re
from typing import Any
import openai
from misroute.config import ModelConfig
from misroute.models.base import Call, Messages, Turn, as_messages

INLINE_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
DEFAULT_PREFIX = '<tool_call>\n{"name": "'


def to_openai(tools: list[dict[str, Any]]) -> list[Any]:
    return [
        {
            "type": "function",
            "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
        }
        for t in tools
    ]


def parse_args(raw: str | None) -> dict[str, Any]:
    try:
        out = json.loads(raw or "{}")
    except ValueError:
        return {"_raw": raw}
    return out if isinstance(out, dict) else {"_value": out}


class OpenAICompatAdapter:
    family = "openai"

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.family = cfg.options.get("family", "openai")
        key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "none"
        self.client = openai.AsyncOpenAI(base_url=cfg.base_url, api_key=key or "none", max_retries=5)

    def _messages(self, user: Messages, system: str | None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        for m in as_messages(user):
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                calls = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["args"])},
                    }
                    for c in m["calls"]
                ]
                out.append({"role": "assistant", "content": m.get("text") or None, "tool_calls": calls})
            else:
                out += [{"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in m["results"]]
        return out

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None) -> Turn:
        kw: dict[str, Any] = {
            "model": self.cfg.model_id,
            "messages": self._messages(user, system),
            "max_tokens": self.cfg.max_tokens,
        }
        if (temp := self.cfg.options.get("temperature", 0)) is not None:
            kw["temperature"] = temp
        if tools:
            kw["tools"] = to_openai(tools)
        if extra := self.cfg.options.get("extra"):
            kw["extra_body"] = extra
        resp = await self.client.chat.completions.create(**kw)
        msg = resp.choices[0].message
        calls = [Call(c.function.name, parse_args(c.function.arguments), c.id) for c in msg.tool_calls or []]
        text = msg.content or ""
        if not calls:
            for m in INLINE_CALL.finditer(text):
                d = parse_args(m.group(1))
                if "name" in d:
                    calls.append(Call(d["name"], d.get("arguments") or {}, f"inline{len(calls)}"))
        usage = resp.usage
        cost = (usage.model_extra or {}).get("cost") if usage else None
        tin, tout = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
        return Turn(calls, text, tin, tout, cost=float(cost) if cost is not None else None)

    async def complete(self, prompt: str, kind: str | None = None) -> str:
        return (await self.chat(prompt, [])).text

    async def complete_turn(self, prompt: str) -> Turn:
        return await self.chat(prompt, [])

    async def _prefill_logprob(self, user: str, tools: list[dict[str, Any]], prefill: str) -> float:
        resp = await self.client.chat.completions.create(
            model=self.cfg.model_id,
            messages=[{"role": "user", "content": user}, {"role": "assistant", "content": prefill}],
            tools=to_openai(tools),
            max_tokens=1,
            temperature=0,
            extra_body={"continue_final_message": True, "add_generation_prompt": False, "prompt_logprobs": 0},
        )
        rows = getattr(resp, "prompt_logprobs", None) or (resp.model_extra or {}).get("prompt_logprobs") or []
        total = 0.0
        for row in rows:
            if row:
                total += max(v["logprob"] for v in row.values())
        return total

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None:
        if not self.cfg.scoring:
            return None
        prefix = self.cfg.options.get("name_prefix", DEFAULT_PREFIX)
        end = self.cfg.options.get("name_suffix", '"')
        base = await self._prefill_logprob(user, tools, prefix)
        logp = {t["name"]: await self._prefill_logprob(user, tools, prefix + t["name"] + end) - base for t in tools}
        top = max(logp.values())
        z = sum(math.exp(v - top) for v in logp.values())
        return {k: math.exp(v - top) / z for k, v in logp.items()}
