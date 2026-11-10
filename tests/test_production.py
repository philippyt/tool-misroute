import json
from misroute.config import Config
from misroute.models.base import Call, Turn
from misroute.modes import code_mode, json_mode
from misroute.project import Project
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool
from misroute.trial import Trial

TOOLS = [Tool("get_case", "A case.", read_only=True)]


class Seen:
    def __init__(self, call):
        self.call, self.systems = call, []

    async def chat(self, user, tools, system=None, rep=0):
        self.systems.append(system)
        return Turn([self.call])


async def test_the_agents_system_prompt_reaches_the_model_in_both_modes():
    model = Seen(Call("get_case", {}, "c"))
    await json_mode.run(model, Trial("m", "json", "get_case", "r", "x"), TOOLS, system="You are Atlas.")
    await json_mode.run(model, Trial("m", "json", "get_case", "r", "x"), TOOLS)
    assert model.systems == ["You are Atlas.", json_mode.SYSTEM]
    coder = Seen(Call("run_code", {"code": "r = await get_case()\nr"}, "c"))
    async with Sandbox(max_processes=1) as box:
        await code_mode.run(coder, box, Trial("m", "code", "get_case", "r", "x"), TOOLS, system="You are Atlas.")
    assert coder.systems[0].startswith("You are Atlas.") and coder.systems[0].endswith(code_mode.SYSTEM)


def write(path, names):
    path.write_text(json.dumps({"tools": [{"name": n, "description": f"{n} tool"} for n in names]}))
    return str(path)


async def test_several_servers_share_one_catalog(tmp_path):
    prompt = tmp_path / "system.md"
    prompt.write_text("You are Atlas.\n", encoding="utf-8")
    cfg = Config.model_validate(
        {
            "servers": [
                {"name": "github", "tools": write(tmp_path / "a.json", ["search_issues", "get_user"])},
                {"name": "jira", "tools": write(tmp_path / "b.json", ["search_issues", "get_sprint"])},
            ],
            "models": ["mock:m"],
            "system_prompt": str(prompt),
        }
    )
    proj = Project(cfg, root=tmp_path / ".misroute")
    tools = await proj.tools()
    assert [(t.name, t.server) for t in tools] == [
        ("search_issues", "github"),
        ("get_user", "github"),
        ("jira_search_issues", "jira"),
        ("get_sprint", "jira"),
    ]
    assert proj.system() == "You are Atlas."
    spots = [{"a": "get_user", "b": "search_issues"}, {"a": "jira_search_issues", "b": "search_issues"}]
    proj.cross_server(spots, tools)
    assert [s["cross_server"] for s in spots] == [False, True]


async def test_one_server_is_unchanged(tmp_path):
    cfg = Config.model_validate({"server": {"tools": write(tmp_path / "a.json", ["get_user"])}, "models": ["mock:m"]})
    proj = Project(cfg, root=tmp_path / ".misroute")
    assert [(t.name, t.server) for t in await proj.tools()] == [("get_user", "")]
    assert proj.system() is None


async def test_compare_two_models_on_the_same_requests(tmp_path):
    from importlib import resources
    import jsonschema
    from misroute import compare
    from misroute.testing.fake_server import app

    confuse = {"get_case_comments": {"get_case_events": 0.9}, "search_cases": {"list_cases": 0.9}}
    cfg = Config.model_validate(
        {
            "server": {"command": ["unused"]},
            "models": [
                {"name": "old", "provider": "mock"},
                {"name": "new", "provider": "mock", "options": {"confuse": confuse}},
            ],
            "generator": "old",
            "validator": "old",
            "requests_per_tool": 12,
            "rotations": 2,
            "repeats": 1,
        }
    )
    proj = Project(cfg, server=app, root=tmp_path)
    res = await compare.compare(proj, "old", "new")
    row = res["modes"][0]
    assert res["verdict"] == row["verdict"] == "worse" and row["diff"] > 0
    assert row["rates"]["new"]["accuracy"] < row["rates"]["old"]["accuracy"]
    assert {t["tool"] for t in row["tools"]} >= {"get_case_comments", "search_cases"}
    assert "get_case_comments|get_case_events" in row["pairs"]["new"] and not row["pairs"]["gone"]
    back = await compare.compare(proj, "new", "old")
    assert back["verdict"] == "better" and back["cost_usd"] == 0
    schema = json.loads(resources.files("misroute.schemas").joinpath("compare.json").read_text(encoding="utf-8"))
    jsonschema.validate(json.loads(json.dumps({"schema_version": 2, "command": "compare", **res})), schema)
    added, names = compare.with_models(cfg, ["old", "ollama:qwen3.5:9b"])
    assert names == ["old", "qwen3.5-9b"] and len(added.models) == 3


def test_real_requests_are_collected_then_confirmed_by_a_person(tmp_path):
    from misroute.requests import generate, real

    tools = [Tool("get_case", "A case.", read_only=True), Tool("update_case", "Change a case.", read_only=False)]
    log = tmp_path / "guard.jsonl"
    lines = [
        {"type": "user", "text": "close case 7"},
        {"type": "call", "tool": "get_case", "args": {}},
        {"type": "call", "tool": "update_case", "args": {}},
        {"type": "event", "kind": "hedge", "tool": "update_case"},
        {"type": "user", "text": "thanks"},
        {"text": "show case 9", "calls": [{"name": "get_case"}]},
        {"request": "open case 3", "tool_calls": [{"function": {"name": "get_case"}}]},
        {"text": "what is the weather", "calls": ["get_weather"]},
    ]
    log.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    found = real.collect(log, tools, [{"text": "show case 9", "tool": "get_case"}])
    assert [(r["text"], r["tool"]) for r in found] == [("close case 7", "update_case"), ("open case 3", "get_case")]
    assert found[0]["called"] == ["get_case", "update_case"] and not found[0]["reviewed"]
    dest = tmp_path / "real.jsonl"
    real.write(dest, [{"text": "show case 9", "tool": "get_case"}, *found])
    assert [r.template for r in generate.load_real(dest, tools)] == ["show case 9"]
    rows = real.read(dest)
    rows[1]["reviewed"] = True
    real.write(dest, rows)
    assert [r.template for r in generate.load_real(dest, tools)] == ["show case 9", "close case 7"]
    state = real.status(rows, tools)
    assert state["reviewed"] == 2 and state["pending"] == 1 and state["per_tool"] == {"get_case": 1, "update_case": 1}


def test_guard_logs_the_user_turn(tmp_path):
    from misroute.config import GuardConfig
    from misroute.guard.rules import Rules
    from misroute.guard.wrapper import Guard
    from misroute.requests import real

    guard = Guard(None, Rules([], set(), GuardConfig()), tmp_path / "guard.jsonl")
    guard.user_turn("close case 7")
    assert guard.before("update_case", {"id": 7}) is None
    assert real.turns(real.read(tmp_path / "guard.jsonl")) == [("close case 7", ["update_case"])]
