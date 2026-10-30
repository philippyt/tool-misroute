from misroute.check import check, decide

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


async def test_reusing_a_held_out_set_scales_p(project):
    project.cfg.requests_per_tool = 2
    project.cfg.slots = 1
    looks = [(await check(project, "get_customer", "Customer contact details and address."))["looks"] for _ in range(3)]
    assert looks == [1, 2, 3]
    res = await check(project, "get_customer", "Customer contact details and address.", refresh=True)
    assert res["looks"] == 1 and res["round"] == 1


def test_decide_multiplies_p_by_looks():
    base = {("m", "json", f"r{i}", "", 0, 0): 1 for i in range(8)}
    better = {k: 0 for k in base}
    assert decide(base, better)["p"] * 2 == decide(base, better, looks=2)["p"]
