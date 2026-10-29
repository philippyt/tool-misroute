import ast
import json
from misroute.detectors.codemode import classify
from misroute.modes.json_mode import decisive, read_first
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
    """Drop pasted stubs of real tools, for recording the intended choice only. None when nothing was a pasted stub."""
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


async def run(
    adapter, sandbox: Sandbox, trial: Trial, tools: list[Tool], max_turns: int = 1, single_call: bool = False
) -> Trial:
    spec = [run_code_tool(tools, single_call)]
    by = {t.name: t for t in tools}
    fakes = FakeTools(tools)
    messages: list[dict] = [{"role": "user", "content": trial.text}]
    programs: list[list[str]] = []
    failures: list[str | None] = []
    first = ""
    for i in range(max_turns):
        turn = await adapter.chat(messages if i else trial.text, spec, SYSTEM, rep=trial.rep)
        code_calls = [c for c in turn.calls if c.name == "run_code"]
        if not code_calls:
            if i == 0:
                trial.calls = [c.name for c in turn.calls if c.name in by]
                trial.failure = "direct_call" if trial.calls else "no_code"
                trial.detail = turn.text[:300]
                return trial
            break
        code = str(code_calls[0].args.get("code", ""))
        start = len(fakes.log)
        res = await sandbox.run(code, fakes.functions())
        log = fakes.log[start:]
        programs.append([r.tool for r in log])
        failures.append(classify(code, res, log, tools))
        if i == 0:
            first = code
            trial.detail = code[:600] + ("\n# " + res.error[:200] if res.error else "")
        if i == max_turns - 1 or any(by[r.tool].is_write for r in log):
            break
        cid = code_calls[0].id or f"code{i}"
        body = json.dumps({"result": res.output, "stdout": res.stdout[:2000], "error": res.error}, default=str)
        messages += [
            {
                "role": "assistant",
                "text": turn.text,
                "calls": [{"id": cid, "name": "run_code", "args": {"code": code}}],
            },
            {"role": "tool", "results": [{"id": cid, "name": "run_code", "content": body[:8000]}]},
        ]
    trial.calls = [n for names in programs for n in names]
    hard = [f for f in failures if f and f != "no_call"]
    trial.failure = hard[0] if hard else (None if trial.calls else "no_call")
    if max_turns > 1:
        trial.decisive = decisive(trial, programs, tools)
    if trial.failure is None and read_first(trial, tools):
        trial.failure = "read_first"
    stripped = strip_stubs(first, set(by)) if failures and failures[0] == "faked" else None
    if stripped:
        again = FakeTools(tools)
        await sandbox.run(stripped, again.functions())
        trial.failure = "stub_copied"
        trial.rerun = again.names()
    return trial
