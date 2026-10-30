from misroute.config import ModelConfig
from misroute.models.anthropic import to_anthropic
from misroute.models.mock import MockAdapter
from misroute.models.openai_compat import OpenAICompatAdapter
from misroute.modes import json_mode
from misroute.server.catalog import Tool
from misroute.trial import Trial

TOOLS = [
    Tool("get_case_events", "Timeline events of a case.", read_only=True),
    Tool("update_case_meta", "Change tags and labels of a case.", read_only=False),
]
MOCK = MockAdapter(
    ModelConfig(name="m", provider="mock", options={"read_first": {"update_case_meta": ["get_case_events"]}})
)


class Wrapped:
    """Accepts the rep argument the cache layer takes."""

    async def chat(self, user, tools, system=None, rep=0):
        return await MOCK.chat(user, tools, system)


def trial(target):
    return Trial("m", "json", target, "r", "please change tags labels for case 7")


async def test_single_turn_marks_read_first():
    t = await json_mode.run(Wrapped(), trial("update_case_meta"), TOOLS, max_turns=1)
    assert t.failure == "read_first" and not t.wrong and t.error


async def test_multi_turn_records_the_write():
    t = await json_mode.run(Wrapped(), trial("update_case_meta"), TOOLS, max_turns=3)
    assert t.calls == ["get_case_events", "update_case_meta"]
    assert t.decisive == ["update_case_meta"] and t.failure is None and not t.error


async def test_multi_turn_read_request_stops_after_answer():
    t = await json_mode.run(Wrapped(), Trial("m", "json", "get_case_events", "r", "timeline events case 7"), TOOLS, 3)
    assert t.calls == ["get_case_events"] and t.decisive == ["get_case_events"] and not t.error


def test_message_conversion():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "text": "", "calls": [{"id": "a1", "name": "get_case_events", "args": {"id": 1}}]},
        {"role": "tool", "results": [{"id": "a1", "name": "get_case_events", "content": "{}"}]},
    ]
    a = to_anthropic(msgs)
    assert a[1]["content"][0]["type"] == "tool_use" and a[2]["content"][0]["tool_use_id"] == "a1"
    o = OpenAICompatAdapter(ModelConfig(name="o", provider="openai-compatible", base_url="http://x"))._messages(
        msgs, "sys"
    )
    assert o[0]["role"] == "system" and o[2]["tool_calls"][0]["id"] == "a1" and o[3]["tool_call_id"] == "a1"


async def test_temperature_dropped_when_rejected():
    import httpx2
    import openai

    calls = []

    class Completions:
        async def create(self, **kw):
            calls.append(kw)
            if "temperature" in kw:
                request = httpx2.Request("POST", "http://x")
                response = httpx2.Response(400, request=request)
                raise openai.BadRequestError(
                    "temperature is not supported for this model", response=response, body=None
                )

            class Msg:
                content, tool_calls = "ok", None

            class Choice:
                message = Msg()

            class Resp:
                choices, usage = [Choice()], None

            return Resp()

    adapter = OpenAICompatAdapter(ModelConfig(name="o", provider="openai-compatible", base_url="http://x"))
    adapter.client = type("C", (), {"chat": type("Ch", (), {"completions": Completions()})()})()
    assert (await adapter.chat("hi", [])).text == "ok"
    assert (await adapter.chat("hi", [])).text == "ok"
    assert ["temperature" in c for c in calls] == [True, False, False]


def test_azure_openai_client(monkeypatch):
    from misroute.models import build

    monkeypatch.setenv("AZ_KEY", "secret")
    cfg = ModelConfig(
        name="az",
        provider="azure-openai",
        model="gpt-deploy",
        base_url="https://x.openai.azure.com",
        api_key_env="AZ_KEY",
        options={"api_version": "2024-10-21"},
    )
    client = build(cfg).client
    assert type(client).__name__ == "AsyncAzureOpenAI" and "x.openai.azure.com" in str(client.base_url)


async def test_cut_off_reply_retries_with_twice_the_tokens_used_and_is_not_cached(tmp_path):
    from misroute.models.base import Budget, Turn
    from misroute.store.cache import Metered

    seen = []

    class Completions:
        async def create(self, **kw):
            seen.append(kw.get("max_tokens"))
            whole = kw.get("max_tokens", 0) >= 20

            class Msg:
                content, tool_calls = ('{"a": 1}' if whole else '{"requests": ["a", "b'), None

            class Choice:
                message, finish_reason = Msg(), "stop" if whole else "length"

            class Usage:
                prompt_tokens, completion_tokens, model_extra = 5, 10, {}

            class Resp:
                choices, usage = [Choice()], Usage()

            return Resp()

    cfg = ModelConfig(name="o", provider="openai-compatible", base_url="http://x")
    adapter = OpenAICompatAdapter(cfg)
    adapter.client = type("C", (), {"chat": type("Ch", (), {"completions": Completions()})()})()
    assert (await adapter.complete_turn("write json")).text == '{"a": 1}'
    assert seen == [None, 20]

    class Cut:
        name, family = "c", "x"

        async def complete_turn(self, prompt):
            return Turn(text='{"requests": ["a", "b', truncated=True)

    m = Metered(Cut(), cfg, Budget(1), tmp_path)
    await m.complete("p")
    assert not list((tmp_path / "cache" / "o").glob("*.json"))


def test_lenient_keeps_complete_requests_from_a_cut_reply():
    from misroute.requests.generate import lenient

    cut = '```json\n{"slot_kind": "id", "slots": ["1", "2"], "requests": ["Show case {slot}", "List the files on'
    assert lenient(cut) == {"slots": ["1", "2"], "requests": ["Show case {slot}"]}
    assert lenient('{"requests": ["x"]}') == {"requests": ["x"]}
