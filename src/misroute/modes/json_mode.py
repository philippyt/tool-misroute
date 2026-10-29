from misroute.server.catalog import Tool
from misroute.trial import Trial

SYSTEM = "You are an assistant that answers by calling the tools provided."


def read_first(trial: Trial, tools: list[Tool]) -> bool:
    """A write request answered only with reads, so the write choice was not observed in this turn."""
    by = {t.name: t for t in tools}
    target = by.get(trial.target)
    return bool(target and target.is_write and trial.calls) and not any(by[c].is_write for c in trial.calls if c in by)


async def run(adapter, trial: Trial, tools: list[Tool]) -> Trial:
    turn = await adapter.chat(trial.text, [t.spec() for t in tools], SYSTEM, rep=trial.rep)
    names = {t.name for t in tools}
    trial.calls = [c.name for c in turn.calls]
    if not turn.calls:
        trial.failure = "no_tool_call"
    elif any(c not in names for c in trial.calls):
        trial.failure = "unknown_tool"
    elif read_first(trial, tools):
        trial.failure = "read_first"
    trial.detail = turn.text[:300]
    return trial
