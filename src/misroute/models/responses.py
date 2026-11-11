import asyncio
import json
import os
from typing import Any
import openai
from misroute.config import ModelConfig
from misroute.models.base import Call, Messages, Turn, as_messages, until_whole
from misroute.models.openai_compat import parse_args

MIN_OUTPUT_TOKENS = 16


def to_responses_tools(tools: list[dict[str, Any]]) -> list[Any]:
    return [
        {
            "type": "function",
            "name": t["name"],
            "description": t["description"],
            "parameters": t["input_schema"],
            "strict": False,
        }
        for t in tools
    ]


def to_input(messages: list[dict[str, Any]]) -> list[Any]:
    out: list[Any] = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            answered = {c["id"] for c in m["calls"]}
            if m.get("raw"):
                out += [
                    item
                    for item in json.loads(m["raw"])
                    if item.get("type") != "function_call" or item.get("call_id") in answered
                ]
                continue
            if m.get("text"):
                out.append({"role": "assistant", "content": m["text"]})
            out += [
                {"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": json.dumps(c["args"])}
                for c in m["calls"]
            ]
        else:
            out += [{"type": "function_call_output", "call_id": r["id"], "output": r["content"]} for r in m["results"]]
    return out


def client(cfg: ModelConfig) -> Any:
    key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "none"
    if cfg.provider != "azure-openai":
        return openai.AsyncOpenAI(base_url=cfg.base_url, api_key=key or "none", max_retries=5)
    if not cfg.base_url:
        raise ValueError("azure-openai needs base_url, the resource endpoint")
    base = cfg.base_url if "/openai/" in cfg.base_url else f"{cfg.base_url.rstrip('/')}/openai/v1/"
    if cfg.options.get("entra"):
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider

        provider = get_bearer_token_provider(DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")

        async def token() -> str:
            return await asyncio.to_thread(provider)

        return openai.AsyncOpenAI(base_url=base, api_key=token, max_retries=5)
    return openai.AsyncOpenAI(base_url=base, api_key=key or "none", max_retries=5)


class ResponsesAdapter:
    family = "openai"

    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg
        self.name = cfg.name
        self.family = cfg.options.get("family", "openai")
        self.client = client(cfg)
        self.encrypted = True

    async def chat(self, user: Messages, tools: list[dict[str, Any]], system: str | None = None) -> Turn:
        return await until_whole(lambda limit: self.once(user, tools, system, limit), self.cfg.max_tokens)

    async def once(
        self, user: Messages, tools: list[dict[str, Any]], system: str | None, max_tokens: int | None
    ) -> Turn:
        kw: dict[str, Any] = {
            "model": self.cfg.model_id,
            "input": to_input(as_messages(user)),
            "store": False,
        }
        if self.encrypted:
            kw["include"] = ["reasoning.encrypted_content"]
        if system:
            kw["instructions"] = system
        if max_tokens:
            kw["max_output_tokens"] = max(max_tokens, MIN_OUTPUT_TOKENS)
        if effort := self.cfg.options.get("reasoning_effort"):
            kw["reasoning"] = {"effort": effort}
        if (temp := self.cfg.options.get("temperature")) is not None:
            kw["temperature"] = temp
        if tools:
            kw["tools"] = to_responses_tools(tools)
        if extra := self.cfg.options.get("extra"):
            kw["extra_body"] = extra
        try:
            resp = await self.client.responses.create(**kw)
        except openai.BadRequestError as e:
            if "include" not in kw or "encrypted" not in str(e).lower():
                raise
            self.encrypted = False
            kw.pop("include")
            resp = await self.client.responses.create(**kw)
        items = list(resp.output or [])
        calls = [Call(i.name, parse_args(i.arguments), i.call_id) for i in items if i.type == "function_call"]
        usage = resp.usage
        details = resp.incomplete_details
        cut = resp.status == "incomplete" and bool(details) and details.reason == "max_output_tokens"
        thought = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        return Turn(
            calls,
            resp.output_text or "",
            usage.input_tokens if usage else 0,
            usage.output_tokens if usage else 0,
            raw=json.dumps([i.model_dump(exclude_none=True) for i in items]),
            truncated=cut,
            reasoning_tokens=thought,
        )

    async def complete(self, prompt: str, kind: str | None = None) -> str:
        return (await self.complete_turn(prompt)).text

    async def complete_turn(self, prompt: str) -> Turn:
        return await self.chat(prompt, [])

    async def score_names(self, user: str, tools: list[dict[str, Any]]) -> dict[str, float] | None:
        return None
