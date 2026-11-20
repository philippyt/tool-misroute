import json
import duckdb
import pytest
from misroute.config import Config
from misroute.project import Project
from misroute.requests.validate import agreement_stats, panel_keep_valid
from misroute.server.catalog import Tool
from misroute.trial import Request


class StubAdapter:
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    async def complete(self, prompt: str, kind: str | None = None, rep: int = 0) -> str:
        self.calls += 1
        return self.reply


def test_config_generators_and_validators_lists():
    cfg = Config.model_validate(
        {
            "server": {"tools": "x"},
            "models": [
                {"name": "claude", "provider": "mock", "options": {"family": "anthropic"}},
                {"name": "gpt", "provider": "mock", "options": {"family": "openai"}},
                {"name": "gemini", "provider": "mock", "options": {"family": "google"}},
                {"name": "gen_a", "provider": "mock", "options": {"family": "anthropic"}},
                {"name": "gen_b", "provider": "mock", "options": {"family": "openai"}},
            ],
            "generators": ["gen_a", "gen_b"],
            "validators": ["gen_a", "gen_b"],
            "writer": "gen_b",
        }
    )
    assert cfg.tested() == ["claude", "gpt", "gemini"]
    assert [m.name for m in cfg.generator_models()] == ["gen_a", "gen_b"]
    assert [m.name for m in cfg.validator_models()] == ["gen_a", "gen_b"]
    # Multi-family generator/validator panels do not warn about single-family overlap with one tested model
    warnings = cfg.role_warnings()
    assert not any("generator" in w for w in warnings)
    assert not any("validator" in w for w in warnings)

    with pytest.raises(ValueError, match="not a configured model"):
        Config.model_validate({"server": {"tools": "x"}, "models": ["mock:m"], "generators": ["missing"]})


async def test_panel_excludes_generator_family_and_checks_consensus():
    t1 = Tool("list_issues", "List open issues")
    t2 = Tool("create_issue", "Create a new issue", destructive=True)
    req = Request("r1", "list_issues", "show me the issues", [], generator_model="g_anth", generator_family="anthropic")

    v_anth = StubAdapter("create_issue")  # would reject, but same family as generator so skipped
    v_oai = StubAdapter("list_issues")
    v_goog = StubAdapter("list_issues")
    panel = [(v_anth, "v_anth", "anthropic"), (v_oai, "v_oai", "openai"), (v_goog, "v_goog", "google")]

    ok, bad, records = await panel_keep_valid(panel, [req], [t1, t2])
    assert len(ok) == 1 and not bad
    assert v_anth.calls == 0
    assert v_oai.calls == 1 and v_goog.calls == 1
    assert records[0]["votes"] == {"v_oai": True, "v_goog": True}

    # With 2 validators, disagreement rejects (requires unanimity)
    v_goog_no = StubAdapter("create_issue")
    panel2 = [(v_anth, "v_anth", "anthropic"), (v_oai, "v_oai", "openai"), (v_goog_no, "v_goog", "google")]
    ok2, bad2, rec2 = await panel_keep_valid(panel2, [req], [t1, t2])
    assert not ok2 and len(bad2) == 1
    assert rec2[0]["passed"] is False

    # With 3 validators, 2 of 3 passes (strict majority)
    req_qwen = Request("r2", "list_issues", "show open issues", [], generator_model="g_qwen", generator_family="qwen")
    v_anth_yes = StubAdapter("list_issues")
    panel3 = [(v_anth_yes, "v_anth", "anthropic"), (v_oai, "v_oai", "openai"), (v_goog_no, "v_goog", "google")]
    ok3, bad3, rec3 = await panel_keep_valid(panel3, [req_qwen], [t1, t2])
    assert len(ok3) == 1 and not bad3 and rec3[0]["passed"] is True

    agg = agreement_stats(records + rec2 + rec3)
    assert agg["requests_judged"] == 3
    assert 0.0 <= agg["unanimous_rate"] <= 1.0
    assert 0.0 <= agg["pairwise_agreement"] <= 1.0


async def test_discover_with_multi_family_panel_and_leave_own_family_out(tmp_path):
    tools_path = tmp_path / "tools.json"
    tools_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "name": "get_issue",
                        "description": "Get details of one issue",
                        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
                    },
                    {
                        "name": "list_issues",
                        "description": "List all issues in a project",
                        "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    cfg = Config.model_validate(
        {
            "server": {"tools": str(tools_path)},
            "models": [
                {"name": "m_anth", "provider": "mock", "options": {"family": "anthropic", "prefix": "kindly"}},
                {"name": "m_oai", "provider": "mock", "options": {"family": "openai", "prefix": "please"}},
                {"name": "m_goog", "provider": "mock", "options": {"family": "google", "prefix": "could"}},
            ],
            "generators": ["m_anth", "m_oai", "m_goog"],
            "validators": ["m_anth", "m_oai", "m_goog"],
            "modes": ["json"],
            "requests_per_tool": 6,
            "rotations": 1,
            "repeats": 1,
        }
    )
    proj = Project(cfg, root=tmp_path / ".misroute")
    res = await proj.discover(models_=["m_anth", "m_oai", "m_goog"], modes=["json"])

    assert res["validator_agreement"]["requests_judged"] > 0
    assert set(res["by_generator_family"]) == {"anthropic", "openai", "google"}
    for cell in res["cells"]:
        assert "other_family" in cell
        assert "own_family" in cell
        assert cell["other_family"]["trials"] + cell["own_family"]["trials"] == cell["trials"]

    # Check DuckDB stored generator_model and generator_family
    with duckdb.connect(str(proj.store.db_path)) as con:
        rows = con.execute("select distinct generator_family from trials where phase = 'discover'").fetchall()
    assert {r[0] for r in rows} == {"anthropic", "openai", "google"}
