import json
from misroute.sandbox.fake_tools import FakeTools
from misroute.server.catalog import Tool
from misroute.trial import Trial

SYSTEM = "You are an assistant that answers by calling the tools provided."


def read_first(trial: Trial, tools: list[Tool]) -> bool:
    """A write request answered only with reads, so the write choice was not observed."""
    by = {t.name: t for t in tools}
    target = by.get(trial.target)
    return bool(target and target.is_write and trial.calls) and not any(by[c].is_write for c in trial.calls if c in by)


def decisive(trial: Trial, turns: list[list[str]], tools: list[Tool]) -> list[str] | None:
    """For write requests the first turn with a write decides, and only its writes count. Otherwise the first turn."""
    by = {t.name: t for t in tools}
    target = by.get(trial.target)
    if not turns:
        return None
    if target and target.is_write:
        for names in turns:
            writes = [n for n in names if n in by and by[n].is_write]
            if writes:
                return writes
    return turns[0]


def results(fakes: FakeTools, calls: list, by: dict[str, Tool]) -> list[dict]:
    out = []
    for c in calls:
        body = fakes.result(by[c.name], c.args)[0] if c.name in by else {"error": f"unknown tool {c.name}"}
        out.append({"id": c.id, "name": c.name, "content": json.dumps(body, default=str)})
    return out


async def run(adapter, trial: Trial, tools: list[Tool], max_turns: int = 1) -> Trial:
    specs = [t.spec() for t in tools]
    by = {t.name: t for t in tools}
    fakes = FakeTools(tools)
    messages: list[dict] = [{"role": "user", "content": trial.text}]
    turns: list[list[str]] = []
    text = ""
    for i in range(max_turns):
        turn = await adapter.chat(messages if i else trial.text, specs, SYSTEM, rep=trial.rep)
        text = turn.text
        names = [c.name for c in turn.calls]
        if not names:
            break
        turns.append(names)
        if i == max_turns - 1 or any(by[n].is_write for n in names if n in by):
            break
        for j, c in enumerate(turn.calls):
            c.id = c.id or f"call{i}_{j}"
        calls = [{"id": c.id, "name": c.name, "args": c.args} for c in turn.calls]
        messages += [
            {"role": "assistant", "text": turn.text, "calls": calls},
            {"role": "tool", "results": results(fakes, turn.calls, by)},
        ]
    trial.calls = [n for names in turns for n in names]
    if max_turns > 1:
        trial.decisive = decisive(trial, turns, tools)
    if not trial.calls:
        trial.failure = "no_tool_call"
    elif any(c not in by for c in trial.calls):
        trial.failure = "unknown_tool"
    elif read_first(trial, tools):
        trial.failure = "read_first"
    trial.detail = text[:300]
    return trial
