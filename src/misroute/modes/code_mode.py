import ast
from misroute.detectors.codemode import classify
from misroute.modes.stubs import run_code_tool
from misroute.sandbox.fake_tools import FakeTools
from misroute.sandbox.monty_runner import Sandbox
from misroute.server.catalog import Tool
from misroute.trial import Trial

SYSTEM = "You are an assistant that answers by writing a Python program for the run_code tool."


def is_stub(node: ast.AST) -> bool:
    body = [n for n in getattr(node, "body", []) if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    return all(isinstance(n, ast.Pass) for n in body)


def strip_stubs(code: str, names: set[str]) -> str | None:
    """Drop pasted stubs of real tools so the program calls the real ones. None when nothing was a pasted stub."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    defs = (ast.FunctionDef, ast.AsyncFunctionDef)
    keep = [n for n in tree.body if not (isinstance(n, defs) and n.name in names and is_stub(n))]
    if len(keep) == len(tree.body):
        return None
    tree.body = keep
    return ast.unparse(tree)


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
    failure = classify(code, res, fakes.log, tools)
    stripped = strip_stubs(code, {t.name for t in tools}) if failure == "faked" else None
    if stripped:
        fakes = FakeTools(tools)
        res = await sandbox.run(stripped, fakes.functions())
        failure = "stub_copied"
    trial.calls = fakes.names()
    trial.failure = failure
    trial.detail = code[:600] + ("\n# " + res.error[:200] if res.error else "")
    return trial
