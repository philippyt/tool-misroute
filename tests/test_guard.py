import json
from mcp import Client
from misroute.config import GuardConfig
from misroute.guard.proxy import build
from misroute.guard.rules import Rules
from misroute.guard.wrapper import Guard, Held, approve, wrap
from misroute.testing.fake_server import app

WRITES = {"update_case", "update_case_meta"}


def rules(**kw):
    return Rules(
        [["update_case", "update_case_meta"], ["get_case_comments", "get_case_events"]], WRITES, GuardConfig(**kw)
    )


def test_rules():
    r = rules()
    assert [e.kind for e in r.check("get_case_events", [("get_case_comments", 0.0)], 1.0)] == ["hedge"]
    assert [e.kind for e in r.check("get_case_events", [("get_case_comments", 0.0)], 10.0)] == ["switch"]
    assert [e.kind for e in r.check("update_case", [("update_case_meta", 0.0)], 1.0)] == ["hedge", "hold"]
    assert r.check("get_customer", [("get_case_comments", 0.0)], 1.0) == []
    assert r.rephrase("show comments on case 5", "show the comments on case 5", 10) is not None


async def test_wrapper_holds_and_approves(tmp_path):
    log = tmp_path / "guard.jsonl"
    async with Client(app) as client:
        g = wrap(client, rules(), log)
        await g.call_tool("update_case", {"case_id": "1", "status": "closed"})
        held = await g.call_tool("update_case_meta", {"case_id": "1", "tags": ["x"]})
        assert isinstance(held, Held)
        approve(log, held.id)
        ok = await g.call_tool("update_case_meta", {"case_id": "1", "tags": ["x"]})
        assert not ok.is_error
    kinds = [json.loads(line)["type"] for line in log.read_text().splitlines()]
    assert kinds.count("held") == 1 and kinds.count("call") == 2


async def test_code_mode_functions(tmp_path):
    calls = []

    async def call(name, args):
        calls.append(name)
        return {"ok": True}

    g = Guard(call, rules(), tmp_path / "g.jsonl")
    fns = g.functions(["get_case_comments", "get_case_events"])
    await fns["get_case_comments"](case_id="1")
    await fns["get_case_events"](case_id="1")
    assert calls == ["get_case_comments", "get_case_events"]
    assert [e.kind for e in g.events] == ["hedge"]
    assert g.program("async def get_case_events():\n    pass", 2, ["get_case_events"]).kind == "faked"


async def test_proxy(tmp_path):
    async with Client(app) as upstream:

        async def call(name, args):
            return await upstream.call_tool(name, args)

        server = build(upstream, Guard(call, rules(), tmp_path / "p.jsonl"))
        async with Client(server) as client:
            tools = await client.list_tools()
            assert len(tools.tools) == 9
            await client.call_tool("update_case", {"case_id": "1", "status": "open"})
            res = await client.call_tool("update_case_meta", {"case_id": "1", "tags": []})
            assert res.is_error and "held" in res.content[0].text
