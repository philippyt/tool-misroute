from misroute.check import MAX_VERDICTS, check

GOOD = "Written comments that caseworkers posted on a case. Not for timeline events such as status changes."


async def test_clause_on_confused_tool_is_better(project):
    project.cfg.requests_per_tool = 16
    await project.discover(modes=["json"])
    res = await check(project, "get_case_comments", GOOD)
    assert res["neighbours"][0] == "get_case_events"
    assert res["held_out"] and res["baseline_kind"] == "neutral"
    assert res["verdict"] == "better", res["reason"]
    assert res["wrong_rate"]["proposed"] < res["wrong_rate"]["baseline"]


async def test_small_sample_is_insufficient(project):
    project.cfg.requests_per_tool = 2
    project.cfg.slots = 1
    res = await check(
        project, "get_customer", "Customer contact details, phone number and postal address for one customer."
    )
    assert res["verdict"] == "insufficient_data"


async def test_held_out_runs_out(project):
    project.cfg.requests_per_tool = 2
    project.cfg.slots = 1
    for _ in range(MAX_VERDICTS):
        res = await check(project, "get_customer", "Customer contact details and address.")
        assert res["held_out"]
    res = await check(project, "get_customer", "Customer contact details and address.")
    assert not res["held_out"] and "refresh" in res["reason"]
    res = await check(project, "get_customer", "Customer contact details and address.", refresh=True)
    assert res["held_out"] and res["round"] == 1
