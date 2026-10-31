from misroute.report.html import render


async def test_lint(project):
    res = await project.lint(draft=True)
    issues = {f["tool"]: f["issue"] for f in res["findings"]}
    assert issues == {"get_attachment": "missing", "get_document": "path only"}
    assert set(res["drafts"]) == set(issues)


async def test_discover_probe_fix_report(project, tmp_path):
    project.cfg.requests_per_tool = 12
    res = await project.discover()
    pairs = {(s["a"], s["b"]): s for s in res["spots"]}
    assert ("get_case_comments", "get_case_events") in pairs
    assert ("list_cases", "search_cases") in pairs
    hedge = pairs[("update_case", "update_case_meta")]
    assert hedge["write"] and hedge["signals"]["hedge"] > 0.4
    code = next(c for c in res["cells"] if c["mode"] == "code")
    assert code["failures"].get("faked", 0) > 0
    assert res["removal"]
    assert res["gate_failed"] and any(s["gate_failed"] for s in res["spots"])
    assert all("near" not in s["signals"] for s in res["spots"])

    probe = await project.probe(pairs=[("get_case_comments", "get_case_events")])
    row = next(r for r in probe["rows"] if r["mode"] == "json")
    assert row["directions"]["get_case_comments>get_case_events"]["wrong"] > 0.2

    fix = await project.fix(pairs=[("get_case_comments", "get_case_events"), ("update_case", "update_case_meta")])
    verdicts = {(r["pair"], r["mode"]): r for r in fix["pairs"]}
    good = verdicts[("get_case_comments|get_case_events", "json")]
    assert good["flagged"] and good["verdict"].startswith("deploy") and good["baseline_kind"] == "neutral"
    assert good["errors"]["clause"] < good["errors"]["baseline"]
    assert good["vs_original"]["p"] < 0.05 and good["errors"]["clause"] < good["errors"]["original"]
    assert verdicts[("update_case|update_case_meta", "json")]["verdict"].startswith("no difference")
    assert all(a["underpowered"] for a in fix["across"])

    html = render(project.store, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "deploy" in html and "get_case_events" in html


async def test_ci_limits(project):
    await project.discover(modes=["json"])
    assert not project.breaches()
    project.cfg.limits.max_write_hedge_rate = 0.05
    assert any("write hedge" in b for b in project.breaches())


async def test_rerun_is_cached(project):
    await project.discover(modes=["json"])
    calls = project.adapter("mock").calls
    await project.discover(modes=["json"])
    assert project.adapter("mock").calls == calls


def test_stale_results_are_skipped(project, tmp_path):
    (project.store.root / "fix.json").write_text('{"pairs": [{"errors": {"sham": 1}}]}')
    assert project.store.read("fix") is None and project.store.stale() == ["fix"]
    assert "older version" in render(project.store, tmp_path / "r.html").read_text(encoding="utf-8")


def test_verdict_answers_the_deploy_question():
    """The Atlas regression case: original 37, neutral baseline 58, clause 40 over 96 trials. Beating the
    baseline proves the content matters; losing to the original means do not deploy."""
    from misroute.project import Project

    def errors(n_bad: int, n: int = 96) -> dict:
        return {("m", "json", f"t{i % 24}", i // 24, 0, 0): i < n_bad for i in range(n)}

    def row(original: int, clause: int) -> dict:
        from misroute.fix.stats import template_test

        base, cl, orig = errors(58), errors(clause), errors(original)
        return {
            "errors": {"original": original, "baseline": 58, "clause": clause},
            "flagged": True,
            **template_test(base, cl),
            "vs_original": template_test(orig, cl),
        }

    atlas, winner = row(37, 40), row(58, 37)
    Project.adjust([atlas, winner])
    assert atlas["verdict"].startswith("do not deploy")
    assert winner["verdict"].startswith("deploy")


async def test_untested_tool_is_marked(project):
    async def none(tool, tools, sibling=None, seed=0):
        return [] if tool.name == "get_customer" else await real(tool, tools, sibling, seed)

    real = project.requests_for
    project.requests_for = none
    res = await project.discover(modes=["json"])
    assert res["untested"] == ["get_customer"]
