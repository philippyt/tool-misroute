import os
from typing import Any
import anthropic
from misroute.config import ModelConfig
from misroute.models.base import Call, Turn


def client(cfg: ModelConfig) -> Any:
    key = os.environ.get(cfg.api_key_env) if cfg.api_key_env else None
    kw: dict[str, Any] = {"max_retries": 5}
    if cfg.base_url:
        kw["base_url"] = cfg.base_url
    if cfg.provider == "anthropic-foundry":
        return anthropic.AsyncAnthropicFoundry(api_key=key, **kw)
    if cfg.provider == "bedrock":
        return anthropic.AsyncAnthropicBedrock(**kw)
    return anthropic.AsyncAnthropic(api_key=key, **kw)


class AnthropicAdapter:
    family = "anthropic"

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.client = client(cfg)

    async def chat(self, user: str, tools: list[dict[str, Any]], system: str | None = None) -> Turn:
        kw: dict[str, Any] = {
            "model": self.cfg.model_id,
            "max_tokens": self.cfg.max_tokens,
            "messages": [{"role": "user", "content": user}],
        }
        if tools:
            kw["tools"] = tools
        if system:
            kw["system"] = system
        kw.update(self.cfg.options.get("extra", {}))
        resp = await self.client.messages.create(**kw)
        calls, text = [], []
        for b in resp.content:
            if b.type == "tool_use":
                calls.append(Call(b.name, dict(b.input or {})))
            elif b.type == "text":
                text.append(b.text)
        return Turn(calls, "".join(text), resp.usage.input_tokens, resp.usage.output_tokens)

    async def complete(self, prompt: str, kind: str | None = None) -> str:
        return (await self.chat(prompt, [])).text

    async def complete_turn(self, prompt: str) -> Turn:
        return await self.chat(prompt, [])

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None:
        return None
