import os
import urllib.request
import pytest
from misroute.config import ModelConfig
from misroute.models.openai_compat import OpenAICompatAdapter
from misroute.server.catalog import Tool

pytestmark = pytest.mark.live
BASE = os.environ.get("MISROUTE_LIVE_URL", "http://localhost:11434/v1")
MODEL = os.environ.get("MISROUTE_LIVE_MODEL", "qwen3.5:9b")
TOOLS = [
    Tool("get_case_comments", "Written comments posted on a case.", {"properties": {"case_id": {"type": "string"}}}),
    Tool("get_case_events", "Timeline events of a case.", {"properties": {"case_id": {"type": "string"}}}),
]


@pytest.fixture
def adapter():
    try:
        urllib.request.urlopen(BASE + "/models", timeout=2)
    except OSError:
        pytest.skip("no local endpoint")
    return OpenAICompatAdapter(ModelConfig(name="live", provider="openai-compatible", model=MODEL, base_url=BASE))


async def test_tool_choice(adapter):
    turn = await adapter.chat("What did the caseworkers write on case 42?", [t.spec() for t in TOOLS])
    assert [c.name for c in turn.calls][:1] == ["get_case_comments"]
