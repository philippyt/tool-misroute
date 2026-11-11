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


async def test_a_pilot_replaces_the_guessed_reply_size(tmp_path):
    from misroute.store.cache import ESTIMATE_TOKENS_OUT
    from misroute.testing.fake_server import app

    cfg = Config.model_validate({"server": {"command": ["unused"]}, "models": ["mock:m"], "modes": ["json"]})
    proj = Project(cfg, server=app, root=tmp_path)
    assert proj.adapter("m").expected_out() == ESTIMATE_TOKENS_OUT
    assert await proj.pilot(["m"], ["json"]) == 3
    assert proj.adapter("m").expected_out() == 20


async def test_requests_written_under_other_instructions_are_not_reused(tmp_path):
    from misroute.testing.fake_server import app

    def project(**more):
        cfg = Config.model_validate({"server": {"command": ["unused"]}, "models": ["mock:m"], **more})
        return Project(cfg, server=app, root=tmp_path)

    proj = project()
    tools = await proj.tools()
    old = {"schema_version": 1}
    for t in tools:
        stale = {"id": "old", "tool": t.name, "template": "stale {slot}", "slots": ["a"]}
        old[t.name] = old[f"{t.name}|description|en"] = [stale] * 6
    (tmp_path / "requests.json").write_text(json.dumps(old), encoding="utf-8")
    reqs = await proj.request_set(tools, tools[:1])
    assert reqs[tools[0].name] and all(r.template != "stale {slot}" for r in reqs[tools[0].name])
    assert json.loads((tmp_path / "requests.json").read_text(encoding="utf-8"))["schema_version"] == 2
    made = proj.adapter("m").calls
    await project().request_set(tools, tools[:1])
    again = project()
    await again.request_set(tools, tools[:1])
    assert again.adapter("m").calls == 0 and proj.adapter("m").calls == made
    assert project(domain="Cases are support tickets.").request_key(tools[0]) != proj.request_key(tools[0])


async def test_the_estimate_uses_measured_turns_per_trial(tmp_path):
    from misroute.testing.fake_server import app

    def project():
        cfg = Config.model_validate(
            {"server": {"command": ["unused"]}, "models": ["mock:m"], "modes": ["json"], "max_turns": 3}
        )
        return Project(cfg, server=app, root=tmp_path)

    proj = project()
    assert proj.turns_per_trial("m") == 3 and not proj.turns_measured(["m"])
    calls_before, _ = await proj.discover_cost(["m"], ["json"])
    assert await proj.pilot(["m"], ["json"]) == 3
    later = project()
    assert later.turns_per_trial("m") < 3 and later.turns_measured(["m"])
    calls_after, _ = await later.discover_cost(["m"], ["json"])
    assert calls_after < calls_before
    later.cfg.max_turns = 2
    later._turns = None
    assert later.turns_per_trial("m") == 2


async def test_a_tool_with_no_valid_requests_gets_more_rounds_with_feedback(tmp_path):
    from misroute.requests import generate

    tools = [Tool("admin_api_catalog", "Routes of the admin API."), Tool("admin_api_request", "Calls a route.")]

    class Helper:
        name, family = "h", "x"

        def __init__(self):
            self.prompts = []

        async def chat(self, user, tools, system=None):
            raise AssertionError

        async def complete(self, prompt, kind=None, rep=0):
            if kind == "validate":
                return "admin_api_catalog" if "which routes" in prompt else "admin_api_request"
            self.prompts.append(prompt)
            text = "which routes exist" if "rejected these earlier" in prompt else "list the repositories"
            return json.dumps({"slots": [], "requests": [text]})

        async def score_names(self, user, tools):
            return None

    cfg = Config.model_validate({"server": {"command": ["unused"]}, "models": ["mock:m"], "requests_per_tool": 1})
    proj = Project(cfg, root=tmp_path)
    helper = Helper()
    proj.role = lambda role: helper
    kept = await proj.requests_in(tools[0], tools, None, 0, "en")
    assert [r.template for r in kept] == ["which routes exist"] and len(helper.prompts) == 2
    assert "- list the repositories (fits admin_api_request better)" in helper.prompts[1]
    assert proj.dropped["admin_api_catalog"][0]["picked"] == "admin_api_request"
    assert generate.feedback([]) == ""


async def test_unread_pages_and_undecided_trials_are_kept_apart_from_failures():
    from misroute.sandbox.monty_runner import Sandbox as Box

    paged = [Tool("list_lines", "Cart lines.", {"properties": {"cursor": {"type": "string"}}}, read_only=True)]
    coder = Seen(Call("run_code", {"code": "r = await list_lines()\nr"}, "c"))
    async with Box(max_processes=1) as box:
        t = await code_mode.run(coder, box, Trial("m", "code", "list_lines", "r", "x"), paged)
    assert t.unread_pages and t.failure is None and not t.error and t.turns == 1 and t.decided
    ended = Trial("m", "json", "update_case", "r", "x", calls=["get_case"], failure="read_first")
    assert ended.error and not ended.decided
