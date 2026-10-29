import json
import sys
import pytest
import yaml
from typer.testing import CliRunner
from misroute.cli import app

runner = CliRunner()


@pytest.fixture
def config(tmp_path):
    def write(price: float = 0.0):
        path = tmp_path / "misroute.yaml"
        data = {
            "server": {"command": [sys.executable, "-m", "misroute.testing.fake_server"]},
            "models": [
                {
                    "name": "mock",
                    "provider": "mock",
                    "price_in": price,
                    "options": {"confuse": {"get_case_comments": {"get_case_events": 0.5}}},
                }
            ],
            "modes": ["json"],
            "rotations": 2,
            "repeats": 1,
            "workdir": str(tmp_path / ".misroute"),
        }
        path.write_text(yaml.safe_dump(data))
        return str(path)

    return write


def call(*args):
    res = runner.invoke(app, list(args))
    return res.exit_code, res.stdout


def test_lint_json(config):
    code, out = call("lint", "-c", config(), "--json")
    data = json.loads(out)
    assert code == 0 and data["schema_version"] == 1 and data["command"] == "lint"
    assert {f["tool"] for f in data["findings"]} == {"get_attachment", "get_document"}


def test_estimate_only_and_max_cost(config):
    path = config(price=1000)
    code, out = call("discover", "-c", path, "--json", "--estimate-only")
    assert code == 0 and json.loads(out)["estimate"]["cost_usd"] > 0
    code, out = call("discover", "-c", path, "--json", "--max-cost", "0.01")
    assert code == 3 and "max cost" in json.loads(out)["error"]
    code, out = call("discover", "-c", path, "--json")
    assert code == 3 and "--yes" in json.loads(out)["error"]


def test_missing_config_is_exit_3(tmp_path):
    code, _ = call("lint", "-c", str(tmp_path / "nope.yaml"), "--json")
    assert code == 3


def test_check_json(config):
    path = config()
    assert call("discover", "-c", path, "--json", "--yes")[0] == 0
    code, out = call(
        "check",
        "-c",
        path,
        "--json",
        "--yes",
        "--tool",
        "get_case_comments",
        "--description",
        "Written comments that caseworkers posted on a case. Not for timeline events.",
    )
    data = json.loads(out)
    assert data["verdict"] in ("better", "no_difference", "insufficient_data", "worse")
    assert code == {"worse": 1, "insufficient_data": 2}.get(data["verdict"], 0)
    assert data["held_out"] and data["trials"] > 0


def test_ci_changed_checks_only_edited_tools(config, tmp_path):
    path = config()
    base = json.loads(call("catalog", "-c", path)[1])
    for t in base["tools"]:
        if t["name"] == "get_case_comments":
            t["description"], t["hash"] = "Comments.", "old"
    (tmp_path / "base.json").write_text(json.dumps(base))
    md = tmp_path / "comment.md"
    args = ["--changed", "--baseline", str(tmp_path / "base.json"), "--markdown", str(md), "--json"]
    _, out = call("ci", "-c", path, *args)
    rows = json.loads(out)["changed"]
    assert [r["tool"] for r in rows] == ["get_case_comments"]
    assert "get_case_comments" in md.read_text()
