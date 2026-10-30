import json
import shutil
from pathlib import Path
import pytest
import yaml
from misroute.config import Config
from misroute.improve import export
from misroute.improve.run import groups, improve
from misroute.project import Project
from misroute.server import openapi

OK = {"200": {"description": "ok"}}
SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Cases", "version": "1"},
    "servers": [{"url": "https://cases.invalid"}],
    "paths": {
        "/api/cases/{id}/comments": {"get": {"operationId": "Cases_GetComments", "responses": OK}},
        "/api/cases/{id}/events": {"get": {"operationId": "Cases_GetEvents", "responses": OK}},
        "/api/customers/{id}": {"get": {"operationId": "Customers_Get", "responses": OK}},
        "/api/health": {"get": {"operationId": "Health_Get", "summary": "Service health check.", "responses": OK}},
    },
}


def project(tmp_path) -> Project:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(SPEC))
    cfg = Config.model_validate(
        {
            "server": {"openapi": str(path)},
            "models": [
                {
                    "name": "mock",
                    "provider": "mock",
                    "options": {"confuse": {"Cases_GetComments": {"Cases_GetEvents": 0.6}}},
                }
            ],
            "modes": ["json"],
            "request_source": "spec",
            "request_languages": ["en", "no"],
            "requests_per_tool": 12,
            "repeats": 1,
            "rotations": 2,
        }
    )
    return Project(cfg, root=tmp_path / ".misroute")


def test_groups_join_flagged_pairs():
    spots = [
        {"a": "a", "b": "b", "errors": 12, "flagged": True},
        {"a": "b", "b": "c", "errors": 7, "flagged": True},
        {"a": "x", "b": "y", "errors": 6, "flagged": True},
        {"a": "y", "b": "z", "errors": 5, "flagged": False},
    ]
    assert groups(spots) == [["b", "a", "c"], ["x", "y"]]
    assert groups(spots, 1) == [["b", "a", "c"]]


async def test_improve_end_to_end(tmp_path):
    proj = project(tmp_path)
    res = await improve(proj, out=tmp_path / "out")
    g = next(g for g in res["groups"] if {"Cases_GetComments", "Cases_GetEvents"} <= set(g["tools"]))
    assert any(c["style"] == "returns" for c in g["dropped"]["Cases_GetComments"])
    assert g["style"]["Cases_GetComments"] == "boundary"
    assert g["confirm"]["tests"]["winner_vs_baseline"]["verdict"] == "better"
    assert g["export"] == "winner"
    by = {e["tool"]: e for e in res["entries"]}
    assert by["Cases_GetComments"]["status"] == "tested" and "Not for" in by["Cases_GetComments"]["description"]
    assert by["Customers_Get"]["status"] == "draft"
    assert by["Health_Get"]["suggest_exclude"]
    assert set(g["confirm"]["by_language"]) == {"en", "no"}
    from importlib import resources
    import jsonschema

    schema = json.loads(resources.files("misroute.schemas").joinpath("improve.json").read_text(encoding="utf-8"))
    jsonschema.validate({"schema_version": 1, "command": "improve", **json.loads(json.dumps(res, default=str))}, schema)

    files = res["files"]
    over = yaml.safe_load(Path(files["overlay"]).read_text(encoding="utf-8"))
    assert over["operations"]["Cases_GetComments"]["description"] == by["Cases_GetComments"]["description"]
    assert set(over["operations"]) == {t["op_id"] for t in res["entries"]}
    actions = yaml.safe_load(Path(files["openapi_overlay"]).read_text(encoding="utf-8"))
    assert actions["overlay"] == "1.0.0"
    assert all(a["target"].startswith("$.paths['/api/") for a in actions["actions"])
    doc = export.apply(json.loads(json.dumps(SPEC)), actions)
    (tmp_path / "applied.json").write_text(json.dumps(doc))
    read_back = {t.name: t.description for t in openapi.tools(tmp_path / "applied.json")}
    assert read_back["Cases_GetComments"] == by["Cases_GetComments"]["description"]
    assert read_back["Customers_Get"] == by["Customers_Get"]["description"]
    assert yaml.safe_load(Path(files["descriptions"]).read_text(encoding="utf-8"))["Customers_Get"]
    files = export.write(tmp_path / "out", res["entries"], proj.cfg.server.openapi, SPEC, fastmcp=True)
    server = Path(files["server"]).read_text(encoding="utf-8")
    assert "fastmcp==4.1.4" in server and "spec.json" in server
    compile(server, "server.py", "exec")


@pytest.mark.fastmcp
@pytest.mark.skipif(not shutil.which("uv"), reason="needs uv")
async def test_generated_fastmcp_server_serves_the_overlay(tmp_path):
    res = await improve(project(tmp_path), out=tmp_path / "out", fastmcp=True)
    served = res["fastmcp"]
    assert served["ok"], served
    assert served["tools"] == len(SPEC["paths"])
    for row in served["held_out"]:
        assert row["error_rate"] == pytest.approx(row["misroute_error_rate"], abs=0.05)


def test_configure_roles_sources_and_inputs(tmp_path):
    from misroute.improve.run import configure, pick_styles

    base = Config.model_validate({"server": {"openapi": "x.json"}, "models": ["ollama:qwen3.5:9b"]})
    cfg, tested = configure(base, ["ollama:qwen3.5:9b"], "openai:gpt-5.6", languages=["no", "en"])
    assert tested == ["qwen3.5-9b"] and cfg.generator == cfg.validator == cfg.writer == "gpt-5.6"
    assert cfg.request_source == "spec" and cfg.request_languages == ["no", "en"] and not cfg.role_warnings()
    kept = Config.model_validate({**base.model_dump(), "request_source": "description"})
    assert configure(kept)[0].request_source == "description"
    assert configure(kept, requests_from="interface")[0].request_source == "spec"
    with pytest.raises(ValueError, match="domain file"):
        configure(base, domain="glossary.txt")
    assert configure(base, domain="Work orders are planned jobs.")[0].domain == "Work orders are planned jobs."
    with pytest.raises(ValueError, match="real requests"):
        configure(base, real_requests=str(tmp_path / "nope.jsonl"))
    assert pick_styles(["minimal", "boundary"]) == ["minimal", "boundary"]
    with pytest.raises(ValueError, match="unknown style"):
        pick_styles(["fancy"])


def test_packaged_guide_matches_repo():
    root = Path(__file__).parent.parent
    for name in ("AGENTS.md", "llms.txt"):
        assert (root / "src/misroute/docs" / name).read_bytes() == (root / name).read_bytes(), name


async def test_review_rewrites_files_from_the_ui(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from misroute.ui.app import create

    monkeypatch.chdir(tmp_path)
    proj = project(tmp_path)
    Path("misroute.yaml").write_text(yaml.safe_dump(proj.cfg.model_dump()), encoding="utf-8")
    res = await improve(proj, out=tmp_path / "out")
    client = TestClient(create(Path("misroute.yaml")))
    assert client.get("/api/improve").json()["result"]["entries"]
    assert 'id="improve"' in client.get("/").text
    edit = "Lists the comments written on one case."
    decisions = {
        "Customers_Get": {"decision": "reject"},
        "Cases_GetComments": {"decision": "edit", "description": edit},
        "Health_Get": {"decision": "pending", "exclude": True},
    }
    assert client.post("/api/improve/review", json={"decisions": decisions}).status_code == 403
    headers = {"x-misroute": "1"}
    bad = client.post("/api/improve/review", json={"decisions": {"nope": {"decision": "accept"}}}, headers=headers)
    assert bad.status_code == 400
    saved = client.post("/api/improve/review", json={"decisions": decisions, "by": "reviewer"}, headers=headers).json()
    assert saved["review"]["counts"]["reject"] == 1 and saved["review"]["counts"]["edit"] == 1
    over = yaml.safe_load(Path(res["files"]["overlay"]).read_text(encoding="utf-8"))["operations"]
    assert over["Customers_Get"]["description"] == over["Customers_Get"]["original"]
    assert over["Customers_Get"]["review"] == "reject"
    assert over["Cases_GetComments"]["description"] == edit and over["Cases_GetComments"]["status"] == "edited"
    assert over["Health_Get"]["exclude"] is True
    plain = yaml.safe_load(Path(res["files"]["descriptions"]).read_text(encoding="utf-8"))
    assert "Customers_Get" not in plain and plain["Cases_GetComments"] == edit
    stored = client.get("/api/improve").json()["result"]
    assert next(e for e in stored["entries"] if e["tool"] == "Cases_GetComments")["review"]["by"] == "reviewer"
    g = stored["groups"][0]
    assert g["confirm"]["examples"] and {"original", "baseline", "winner"} <= set(g["confirm"]["examples"][0]["picked"])


async def test_baseline_survives_a_writer_without_json():
    from misroute.fix import clause
    from misroute.improve.run import Improver
    from misroute.server.catalog import Tool

    class Writer:
        def __init__(self, replies):
            self.replies = list(replies)

        async def complete(self, prompt, kind=None, rep=0):
            return self.replies.pop(0) if self.replies else "Returns the comments on a case."

    tool = Tool("get_comments", "Returns comments for a case id.")
    short, longer = '{"a": "Lists comments."}', '{"a": "Lists the comments written on one case by caseworkers."}'
    assert clause.fit("Lists the comments written on one case by caseworkers.", 30) == "Lists the comments written on."
    assert await clause.neutral_one(Writer([short, longer]), tool, 30) == "Lists the comments written on."
    assert await clause.neutral_one(Writer([short, short]), tool, 30) == "Lists comments."
    with pytest.raises(ValueError):
        await clause.neutral_one(Writer(["nope"]), tool, 30)

    class Proj:
        def role(self, name):
            return Writer(["nope", "nope", "nope"])

    texts, kinds = await Improver(Proj(), [], []).baseline([tool], ["get_comments"], {"get_comments": "x" * 30})
    assert texts["get_comments"] and kinds["get_comments"].startswith("draft")
