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
    kept, picks = await validate.keep_valid(judge, [request()], TOOLS)
    assert not kept and picks == [None]
    assert "reply none" in judge.prompts[0] and "Domain:" not in judge.prompts[0]


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
    assert "placeholder.\n\nTool: azure_change" in judge.prompts[0]


def test_lint_reports_tools_with_an_unknown_effect(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tools = [{"name": "azure_catalog", "description": "Lists the operations azure_change can run on a resource."}]
    (tmp_path / "tools.json").write_text(json.dumps({"tools": tools}))
    (tmp_path / "misroute.yaml").write_text(yaml.safe_dump({"server": {"tools": "tools.json"}, "models": ["mock:m"]}))
    runner = CliRunner()
    assert json.loads(runner.invoke(app, ["lint", "--json"]).stdout)["effect_unknown"] == ["azure_catalog"]
    assert "readOnlyHint" in " ".join(runner.invoke(app, ["lint"]).stdout.split())
