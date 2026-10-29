from misroute.detectors.codemode import classify
from misroute.modes.stubs import run_code_tool
from misroute.sandbox.fake_tools import FakeTools
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool
from misroute.trial import Trial

SYSTEM = "You are an assistant that answers by writing a Python program for the run_code tool."


async def run(adapter, sandbox: Sandbox, trial: Trial, tools: list[Tool], single_call: bool = False) -> Trial:
    turn = await adapter.chat(trial.text, [run_code_tool(tools, single_call)], SYSTEM, rep=trial.rep)
    code_calls = [c for c in turn.calls if c.name == "run_code"]
    if not code_calls:
        names = {t.name for t in tools}
        trial.calls = [c.name for c in turn.calls if c.name in names]
        trial.failure = "direct_call" if trial.calls else "no_code"
        trial.detail = turn.text[:300]
        return trial
    code = str(code_calls[0].args.get("code", ""))
    fakes = FakeTools(tools)
    res = await sandbox.run(code, fakes.functions())
    trial.calls = fakes.names()
    trial.failure = classify(code, res, fakes.log, tools)
    trial.detail = code[:600] + ("\n# " + res.error[:200] if res.error else "")
    return trial
