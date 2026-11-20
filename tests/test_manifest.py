import json
from misroute.config import Config
from misroute.project import Project
from misroute.store.manifest import (
    load_requests,
    sample_for_audit,
    save_requests,
    score_audit,
    write_audit_jsonl,
)
from misroute.trial import Request, Trial


async def test_manifest_and_frozen_requests(tmp_path):
    srv_dir = tmp_path / "srv"
    srv_dir.mkdir()
    tools_path = srv_dir / "tools.json"
    tools_path.write_text(
        json.dumps(
            {
                "tools": [
                    {
                        "name": "get_issue",
                        "description": "Fetch a single issue by id",
                        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
                    },
                    {
                        "name": "list_issues",
                        "description": "List all issues",
                        "inputSchema": {"type": "object", "properties": {}},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    (srv_dir / "meta.json").write_text(
        json.dumps({"name": "demo", "repo_url": "https://example.com/demo", "source_ref": "abc1234"}),
        encoding="utf-8",
    )

    frozen_path = tmp_path / "frozen.jsonl"
    reqs = {
        "get_issue": [
            Request("r1", "get_issue", "show issue {slot}", ["42"], generator_model="g1", generator_family="openai")
        ],
        "list_issues": [
            Request("r2", "list_issues", "list all open issues", [], generator_model="g2", generator_family="google")
        ],
    }
    digest = save_requests(frozen_path, reqs)
    assert len(digest) == 64
    reloaded = load_requests(frozen_path)
    assert set(reloaded) == {"get_issue", "list_issues"}
    assert reloaded["get_issue"][0].generator_family == "openai"

    cfg = Config.model_validate(
        {
            "server": {"tools": str(tools_path)},
            "frozen_requests": str(frozen_path),
            "models": [{"name": "m1", "provider": "mock", "options": {"family": "anthropic"}}],
            "modes": ["json"],
            "rotations": 1,
            "repeats": 1,
        }
    )
    proj = Project(cfg, root=tmp_path / ".misroute")
    res = await proj.discover()

    manifest = res["manifest"]
    assert manifest["misroute_version"]
    assert manifest["date_utc"]
    assert manifest["server"]["meta"]["source_ref"] == "abc1234"
    assert manifest["server"]["tools_sha256"]
    assert manifest["server"]["frozen_requests_sha256"] == digest
    assert manifest["tool_count"] == 2
    assert res["request_use"]["written"] == 0
    assert res["request_use"]["reused"] == 2


def test_stratified_human_audit_and_scoring(tmp_path):
    trials = []
    # 3 confused templates with different error rates + 4 clean templates
    for i in range(3):
        for m in ("m1", "m2", "m3"):
            err = i == 0 or (i == 1 and m != "m3") or (i == 2 and m == "m1")
            trials.append(Trial(m, "json", "t_a", f"bad_{i}", f"bad req {i}", calls=["t_b" if err else "t_a"]))
    for i in range(4):
        for m in ("m1", "m2", "m3"):
            trials.append(Trial(m, "json", "t_a", f"ok_{i}", f"ok req {i}", calls=["t_a"]))

    sample = sample_for_audit(trials, top_confused=2, random_clean=2, seed=0)
    assert len(sample) == 4
    confused = [r for r in sample if r["stratum"] == "confused"]
    clean = [r for r in sample if r["stratum"] == "clean"]
    assert len(confused) == 2 and len(clean) == 2
    assert confused[0]["request_id"] == "bad_0"
    assert confused[0]["error_rate"] == 1.0
    assert confused[0]["top_wrong_pick"] == "t_b"

    # Simulate human labeling
    confused[0]["label"] = "valid_misroute"
    confused[1]["label"] = "ambiguous_request"
    clean[0]["label"] = "valid_clean"
    clean[1]["label"] = "valid_clean"

    out_path = write_audit_jsonl(tmp_path / "audit.jsonl", sample)
    assert out_path.exists()
    summary = score_audit(sample)
    assert summary["labeled"] == 4
    assert summary["audited_precision"] == 0.5
    assert summary["label_error_rate"] == 0.25
    assert 0.0 <= summary["audited_precision_ci95"][0] < summary["audited_precision_ci95"][1] <= 1.0
