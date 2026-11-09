import json
from types import SimpleNamespace
import httpx2
import openai
import pytest
from misroute.config import ModelConfig
from misroute.doctor import hint
from misroute.models import build
from misroute.models.base import Budget
from misroute.models.responses import ResponsesAdapter, to_input, to_responses_tools
from misroute.modes import code_mode, json_mode
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool
from misroute.store.cache import Metered
from misroute.trial import Trial

CATALOG = Tool("azure_catalog", "Lists operations.", {"type": "object", "properties": {}}, read_only=True)
CHANGE = Tool("azure_change", "Applies an operation.", {"type": "object", "properties": {}}, read_only=False)
TOOLS = [CATALOG, CHANGE]


def item(**kw):
    return SimpleNamespace(**kw, model_dump=lambda exclude_none=True: kw)


def call_item(name, call_id):
    return item(type="function_call", name=name, arguments="{}", call_id=call_id, id=f"fc_{call_id}")


def reply(*items, text="", status="completed", reason=None, tokens=50):
    return SimpleNamespace(
        output=list(items),
        output_text=text,
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        usage=SimpleNamespace(input_tokens=100, output_tokens=tokens),
    )


def adapter(*replies, **options):
    cfg = ModelConfig(
        name="astra",
        provider="azure-openai",
        model="gpt-6-astra",
        base_url="https://x.openai.azure.com",
        price_in=1,
        price_out=1,
        options={"api": "responses", **options},
    )
    a = ResponsesAdapter(cfg)
    a.seen = []
    queue = list(replies)

    async def create(**kw):
        a.seen.append(kw)
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    a.client = SimpleNamespace(responses=SimpleNamespace(create=create))
    return a


def test_build_picks_the_adapter_only_when_asked(monkeypatch):
    monkeypatch.setenv("AZ_KEY", "secret")
    base = {"provider": "azure-openai", "model": "d", "base_url": "https://x.openai.azure.com", "api_key_env": "AZ_KEY"}
    plain = build(ModelConfig(name="a", **base))
    assert type(plain).__name__ == "OpenAICompatAdapter"
    made = build(ModelConfig(name="a", options={"api": "responses"}, **base))
    assert type(made).__name__ == "ResponsesAdapter"
    assert str(made.client.base_url) == "https://x.openai.azure.com/openai/v1/"
    assert made.client.api_key == "secret"


def test_an_explicit_openai_path_is_kept():
    cfg = ModelConfig(
        name="a",
        provider="azure-openai",
        model="d",
        base_url="https://x.openai.azure.com/openai/v2/",
        options={"api": "responses"},
    )
    assert str(build(cfg).client.base_url) == "https://x.openai.azure.com/openai/v2/"


def test_tools_are_flat_and_not_strict():
    [tool] = to_responses_tools([CHANGE.spec()])
    assert tool["type"] == "function" and tool["name"] == "azure_change" and tool["strict"] is False
    assert "function" not in tool and tool["parameters"]["type"] == "object"


async def test_a_reply_becomes_a_turn_and_effort_is_sent():
    a = adapter(
        reply(item(type="reasoning", id="rs_1"), call_item("azure_change", "c1"), tokens=70), reasoning_effort="low"
    )
    turn = await a.chat("hi", [CHANGE.spec()], "be brief")
    sent = a.seen[0]
    assert sent["reasoning"] == {"effort": "low"} and sent["instructions"] == "be brief" and sent["store"] is False
    assert "temperature" not in sent and "max_output_tokens" not in sent
    assert turn.calls[0].name == "azure_change" and turn.calls[0].id == "c1"
    assert (turn.tokens_in, turn.tokens_out) == (100, 70)
    assert [i["type"] for i in json.loads(turn.raw)] == ["reasoning", "function_call"]


async def test_cut_reply_retries_with_more_tokens_and_bills_both():
    a = adapter(reply(status="incomplete", reason="max_output_tokens", tokens=40), reply(text="whole", tokens=60))
    turn = await a.chat("hi", [])
    assert [k.get("max_output_tokens") for k in a.seen] == [None, 80]
    assert turn.text == "whole" and turn.tokens_out == 100 and turn.tokens_in == 200


async def test_encrypted_reasoning_is_dropped_when_the_model_rejects_it():
    request = httpx2.Request("POST", "http://x")
    refused = openai.BadRequestError(
        "Encrypted content is not supported with this model.",
        response=httpx2.Response(400, request=request),
        body=None,
    )
    a = adapter(refused, reply(text="one"), reply(text="two"))
    assert (await a.chat("hi", [])).text == "one"
    assert (await a.chat("hi", [])).text == "two"
    assert ["include" in k for k in a.seen] == [True, False, False]


async def test_other_bad_requests_are_not_hidden():
    request = httpx2.Request("POST", "http://x")
    refused = openai.BadRequestError("bad tool", response=httpx2.Response(400, request=request), body=None)
    with pytest.raises(openai.BadRequestError):
        await adapter(refused).chat("hi", [])


def test_without_raw_messages_are_rebuilt():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "text": "looking", "calls": [{"id": "a", "name": "n", "args": {"x": 1}}]},
        {"role": "tool", "results": [{"id": "a", "name": "n", "content": "{}"}]},
    ]
    out = to_input(msgs)
    assert out[1] == {"role": "assistant", "content": "looking"}
    assert out[2]["type"] == "function_call" and out[2]["call_id"] == "a" and out[2]["arguments"] == '{"x": 1}'
    assert out[3] == {"type": "function_call_output", "call_id": "a", "output": "{}"}


def test_raw_drops_calls_that_get_no_answer():
    raw = json.dumps(
        [
            {"type": "reasoning", "id": "rs"},
            {"type": "function_call", "call_id": "a"},
            {"type": "function_call", "call_id": "b"},
        ]
    )
    out = to_input([{"role": "assistant", "text": "", "calls": [{"id": "a", "name": "n", "args": {}}], "raw": raw}])
    assert [i.get("call_id", i["type"]) for i in out] == ["reasoning", "a"]


async def test_reasoning_items_go_back_on_the_next_turn(tmp_path):
    first = reply(item(type="reasoning", id="rs_1", encrypted_content="sealed"), call_item("azure_catalog", "c1"))
    a = adapter(first, reply(call_item("azure_change", "c2")))
    m = Metered(a, a.cfg, Budget(10), tmp_path)
    t = await json_mode.run(m, Trial("astra", "json", "azure_change", "r", "suspend it"), TOOLS, max_turns=2)
    assert t.calls == ["azure_catalog", "azure_change"] and not t.error
    second = a.seen[1]["input"]
    assert second[1] == {"type": "reasoning", "id": "rs_1", "encrypted_content": "sealed"}
    assert second[2]["call_id"] == "c1" and second[3]["type"] == "function_call_output"


async def test_a_cached_first_turn_still_carries_its_reasoning(tmp_path):
    first = reply(item(type="reasoning", id="rs_1", encrypted_content="sealed"), call_item("azure_catalog", "c1"))
    a = adapter(first, reply(call_item("azure_change", "c2")))
    m = Metered(a, a.cfg, Budget(10), tmp_path)
    await json_mode.run(m, Trial("astra", "json", "azure_change", "r", "suspend it"), TOOLS, max_turns=2)
    b = adapter()
    again = Metered(b, b.cfg, Budget(10), tmp_path)
    t = await json_mode.run(again, Trial("astra", "json", "azure_change", "r", "suspend it"), TOOLS, max_turns=2)
    assert t.calls == ["azure_catalog", "azure_change"] and again.hits == 2 and not b.seen


async def test_code_mode_runs_through_the_adapter(tmp_path):
    code = "a = await azure_catalog()\nb = await azure_change()\nb"
    run = item(type="function_call", name="run_code", arguments=json.dumps({"code": code}), call_id="c1")
    a = adapter(reply(run))
    m = Metered(a, a.cfg, Budget(10), tmp_path)
    async with Sandbox() as box:
        t = await code_mode.run(m, box, Trial("astra", "code", "azure_change", "r", "suspend it"), TOOLS)
    assert t.calls == ["azure_catalog", "azure_change"]
    assert a.seen[0]["tools"][0]["name"] == "run_code"


def test_doctor_names_the_fix_for_the_real_astra_error():
    err = (
        "BadRequestError: Error code: 400 - Function tools with reasoning_effort are not supported for gpt-6-astra "
        "in /v1/chat/completions. To use function tools, use /v1/responses or set reasoning_effort to 'none'."
    )
    assert "api: responses" in hint(err)


async def test_doctor_tries_a_tool_call_and_reports_the_fix(tmp_path, monkeypatch):
    from misroute import doctor

    class ToolsRefused:
        async def chat(self, user, tools, system=None):
            if tools:
                raise ValueError("Function tools are not supported in /v1/chat/completions, use /v1/responses")
            return SimpleNamespace(text="ok", calls=[])

    monkeypatch.setattr(doctor, "build", lambda m: ToolsRefused())
    (tmp_path / "t.json").write_text('{"tools": [{"name": "a", "description": "A."}]}')
    (tmp_path / "misroute.yaml").write_text(
        f"server: {{tools: {(tmp_path / 't.json').as_posix()}}}\nmodels:\n  - {{name: m, provider: mock}}\n"
    )
    rows = {r["check"]: r for r in await doctor.run(tmp_path / "misroute.yaml")}
    assert rows["m call"]["ok"] and not rows["m tools"]["ok"]
    assert "api: responses" in rows["m tools"]["fix"]
