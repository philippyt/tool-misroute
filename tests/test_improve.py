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


def test_groups_join_connected_pairs():
    spots = [
        {"a": "a", "b": "b", "score": 1.0, "signals": {"wrong": 0.5}},
        {"a": "b", "b": "c", "score": 0.5, "signals": {"hedge": 0.2}},
        {"a": "x", "b": "y", "score": 0.4, "signals": {"wrong": 0.1}},
        {"a": "y", "b": "z", "score": 0.2, "signals": {"wrong": 0.1}},
        {"a": "p", "b": "q", "score": 0.9, "signals": {"removal": 1.0}},
    ]
    assert groups(spots, 5) == [["b", "a", "c"], ["x", "y"]]
    assert groups(spots, 1, max_size=2) == [["b", "a"]]


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
