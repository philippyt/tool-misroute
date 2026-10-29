from misroute.server.catalog import Tool
from misroute.trial import Trial

SYSTEM = "You are an assistant that answers by calling the tools provided."


async def run(adapter, trial: Trial, tools: list[Tool]) -> Trial:
    turn = await adapter.chat(trial.text, [t.spec() for t in tools], SYSTEM, rep=trial.rep)
    names = {t.name for t in tools}
    trial.calls = [c.name for c in turn.calls]
    if not turn.calls:
        trial.failure = "no_tool_call"
    elif any(c not in names for c in trial.calls):
        trial.failure = "unknown_tool"
    trial.detail = turn.text[:300]
    return trial
