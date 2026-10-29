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
