import json
import yaml
from typer.testing import CliRunner
from misroute.cli import app
from misroute.models.base import Call, Turn
from misroute.modes import code_mode, json_mode
from misroute.requests import generate, validate
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool, effect_unknown
from misroute.trial import Request, Trial

SCHEMA = {"type": "object", "properties": {}}
CATALOG = Tool("azure_catalog", "Lists operations.", SCHEMA, read_only=True)
CHANGE = Tool("azure_change", "Applies an operation.", SCHEMA, read_only=False)
OTHER = Tool("azure_other", "Applies something else.", SCHEMA, read_only=False)
TOOLS = [CATALOG, CHANGE, OTHER]


class Script:
    def __init__(self, *names):
        self.names = names

    async def chat(self, user, tools, system=None, rep=0):
        return Turn([Call(n, {}, f"c{i}") for i, n in enumerate(self.names)])


def trial(target, mode="json"):
    return Trial("m", mode, target, "r", "x")


async def test_a_read_before_the_write_in_one_turn_is_not_confusion():
    t = await json_mode.run(Script("azure_catalog", "azure_change"), trial("azure_change"), TOOLS)
    assert t.picks == ["azure_change"] and not t.error and not t.wrong and not t.hedge


async def test_two_writes_in_one_turn_are_still_a_hedge():
    t = await json_mode.run(Script("azure_change", "azure_other"), trial("azure_change"), TOOLS)
    assert t.hedge and t.error


async def test_the_wrong_write_after_a_read_is_still_wrong():
    t = await json_mode.run(Script("azure_catalog", "azure_other"), trial("azure_change"), TOOLS)
    assert t.picks == ["azure_other"] and t.wrong and t.error


async def test_a_read_request_that_also_writes_is_still_an_error():
    t = await json_mode.run(Script("azure_catalog", "azure_change"), trial("azure_catalog"), TOOLS)
    assert t.hedge and t.error


async def code_trial(code, target):
    class Coder:
        async def chat(self, user, tools, system=None, rep=0):
            return Turn([Call("run_code", {"code": code}, "c")])

    async with Sandbox() as box:
        return await code_mode.run(Coder(), box, trial(target, "code"), TOOLS)


async def test_one_program_that_reads_then_writes_is_not_confusion():
    t = await code_trial("a = await azure_catalog()\nb = await azure_change()\nb", "azure_change")
    assert t.calls == ["azure_catalog", "azure_change"] and t.picks == ["azure_change"]
    assert not t.error and t.failure is None


async def test_one_program_that_reads_then_writes_the_wrong_tool_is_wrong():
    t = await code_trial("a = await azure_catalog()\nb = await azure_other()\nb", "azure_change")
    assert t.picks == ["azure_other"] and t.wrong


def test_tools_that_say_change_count_as_writes():
    assert Tool("azure_change").is_write and Tool("modify_user").is_write and not Tool("azure_catalog").is_write


def test_effect_unknown_lists_only_tools_nothing_explains():
    tools = [
        Tool("azure_catalog"),
        Tool("get_user"),
        Tool("azure_change"),
        Tool("azure_thing", read_only=True),
        Tool("azure_other", destructive=False),
    ]
    assert effect_unknown(tools) == ["azure_catalog"]


class Judge:
    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    async def complete(self, prompt, kind=None, rep=0):
        self.prompts.append(prompt)
        return self.reply


def request():
    return Request("r", "azure_change", "suspend user {slot}", ["7"])


async def test_validator_can_reject_a_request_no_tool_can_do():
    judge = Judge("none")
    kept, bad = await validate.keep_valid(judge, [request()], TOOLS)
    assert not kept and bad == [{"request": "suspend user 7", "picked": None, "reason": ""}]
    assert "Reply none" in judge.prompts[0] and "Domain:" not in judge.prompts[0]


async def test_a_request_that_needs_two_tools_is_rejected_with_both_named():
    judge = Judge("azure_change, azure_other")
    kept, bad = await validate.keep_valid(judge, [request()], TOOLS)
    assert not kept and bad[0]["picked"] == "azure_other"
    assert bad[0]["reason"] == "asks for more than one thing, needs azure_change and azure_other"
    looked = Judge("azure_catalog, azure_change")
    kept, _ = await validate.keep_valid(looked, [request()], TOOLS, lookups=frozenset({"azure_catalog"}))
    assert kept
    kept, bad = await validate.keep_valid(Judge("azure_other"), [request()], TOOLS)
    assert not kept and bad[0] == {"request": "suspend user 7", "picked": "azure_other", "reason": ""}
    assert "everything the request asks for" in judge.prompts[0]


async def test_validator_gives_the_reason_a_request_is_unfair():
    judge = Judge("none: azure_change has no project filter")
    kept, bad = await validate.keep_valid(judge, [request()], TOOLS)
    assert not kept and bad[0]["picked"] is None and bad[0]["reason"] == "azure_change has no project filter"
    for unfair in ("parameters and returned fields do not support", "a cursor or a next page"):
        assert unfair in judge.prompts[0]


async def test_generator_does_not_force_a_placeholder_on_a_tool_without_one():
    judge = Judge('{"slots": [], "requests": ["how much storage is in use", "usage for {slot}"]}')
    reqs = await generate.generate(judge, Tool("admin_get_usage_statistics", "Usage totals."), 2, 4)
    assert [r.template for r in reqs] == ["how much storage is in use"]
    assert reqs[0].texts() == [("", "how much storage is in use")]
    assert "without a placeholder" in judge.prompts[0] and "a next page or a cursor" in judge.prompts[0]


async def test_validator_sees_the_domain_and_keeps_a_good_request():
    judge = Judge("azure_change")
    kept, _ = await validate.keep_valid(judge, [request()], TOOLS, domain="Allowed operations: restart a VM.")
    assert kept and "Domain: Allowed operations: restart a VM." in judge.prompts[0]


async def test_generator_sees_the_domain_from_descriptions_too():
    judge = Judge('{"slots": ["7"], "requests": ["restart vm {slot}"]}')
    sibling = Tool("azure_other", "Applies something else.")
    await generate.generate(judge, CHANGE, 1, 1, domain="Allowed operations: restart a VM.")
    await generate.generate(judge, CHANGE, 1, 1, sibling, domain="Allowed operations: restart a VM.")
    assert all("Domain: Allowed operations: restart a VM." in p for p in judge.prompts)


async def test_generator_prompt_is_unchanged_without_a_domain():
    judge = Judge('{"slots": ["7"], "requests": ["restart vm {slot}"]}')
    await generate.generate(judge, CHANGE, 1, 1)
    assert "a cursor.\n\nTool: azure_change" in judge.prompts[0]


def test_lint_reports_tools_with_an_unknown_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tools = [{"name": "azure_catalog", "description": "Lists the operations azure_change can run on a resource."}]
    (tmp_path / "tools.json").write_text(json.dumps({"tools": tools}))
    (tmp_path / "misroute.yaml").write_text(yaml.safe_dump({"server": {"tools": "tools.json"}, "models": ["mock:m"]}))
    runner = CliRunner()
    assert json.loads(runner.invoke(app, ["lint", "--json"]).stdout)["effect_unknown"] == ["azure_catalog"]
    assert "readOnlyHint" in " ".join(runner.invoke(app, ["lint"]).stdout.split())


READ = Tool("azure_read", "Reads a resource.", SCHEMA, read_only=True)
LOGS = Tool("azure_logs", "Reads logs.", SCHEMA, read_only=True)
READS = [CATALOG, READ, LOGS, CHANGE]
LOOKUPS = frozenset({"azure_catalog"})


class Turns:
    def __init__(self, *turns):
        self.turns, self.seen = list(turns), 0

    async def chat(self, user, tools, system=None, rep=0):
        names = self.turns[min(self.seen, len(self.turns) - 1)]
        self.seen += 1
        return Turn([Call(n, {}, f"c{i}") for i, n in enumerate(names)])


async def test_a_lookup_before_the_read_is_not_a_choice():
    model = Turns(["azure_catalog"], ["azure_read"])
    t = await json_mode.run(model, trial("azure_read"), READS, 2, lookups=LOOKUPS)
    assert t.calls == ["azure_catalog", "azure_read"] and t.picks == ["azure_read"]
    assert not t.error and t.failure is None and t.reached
    plain = await json_mode.run(Turns(["azure_catalog"], ["azure_read"]), trial("azure_read"), READS, 2)
    assert plain.picks == ["azure_catalog"] and plain.wrong and plain.reached


async def test_the_wrong_read_after_a_lookup_is_still_wrong():
    model = Turns(["azure_catalog"], ["azure_logs"], ["azure_read"])
    t = await json_mode.run(model, trial("azure_read"), READS, 3, lookups=LOOKUPS)
    assert t.picks == ["azure_logs"] and t.wrong and t.reached


async def test_only_lookups_leaves_the_choice_unobserved():
    t = await json_mode.run(Turns(["azure_catalog"]), trial("azure_read"), READS, 2, lookups=LOOKUPS)
    assert t.failure == "lookup_only" and t.error and not t.wrong and not t.reached
    own = await json_mode.run(Turns(["azure_catalog"]), trial("azure_catalog"), READS, 2, lookups=LOOKUPS)
    assert own.picks == ["azure_catalog"] and not own.error


async def test_a_trial_stops_once_the_target_is_called():
    model = Turns(["azure_read"], ["azure_logs"])
    t = await json_mode.run(model, trial("azure_read"), READS, 3)
    assert model.seen == 1 and t.calls == ["azure_read"] and not t.error
    gone = Turns(["azure_logs"], ["azure_logs"])
    await json_mode.run(gone, trial("azure_read"), [CATALOG, LOGS], 3)
    assert gone.seen == 1


async def test_a_lookup_inside_one_program_is_not_a_choice():
    class Coder:
        async def chat(self, user, tools, system=None, rep=0):
            return Turn([Call("run_code", {"code": "a = await azure_catalog()\nb = await azure_read()\nb"}, "c")])

    async with Sandbox() as box:
        t = await code_mode.run(Coder(), box, trial("azure_read", "code"), READS, lookups=LOOKUPS)
        plain = await code_mode.run(Coder(), box, trial("azure_read", "code"), READS)
    assert t.picks == ["azure_read"] and not t.error
    assert plain.hedge and not plain.wrong


def test_tools_that_lead_to_several_targets_are_lookup_candidates():
    from misroute.detectors.rank import lookup_candidates

    def led(target, first, i):
        return Trial("m", "json", target, f"{target}{i}", "x", calls=[first, target], decisive=[first])

    ts = [led("azure_read", "azure_catalog", i) for i in range(4)] + [led("azure_logs", "azure_catalog", 9)] * 2
    ts += [led("azure_read", "azure_logs", i) for i in range(8)]
    found = lookup_candidates(ts)
    assert [r["tool"] for r in found] == ["azure_catalog"] and found[0]["trials"] == 6
    assert found[0]["targets"] == ["azure_logs", "azure_read"]
    assert lookup_candidates(ts, LOOKUPS) == []
