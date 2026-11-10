import json
import sys
from pathlib import Path
import pytest
import yaml
from typer.testing import CliRunner
from misroute.cli import Opts, app, load

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
    assert code == 0 and data["schema_version"] == 2 and data["command"] == "lint"
    assert {f["tool"] for f in data["findings"]} == {"get_attachment", "get_document"}


def test_estimate_only_and_max_cost(config):
    path = config(price=1000)
    code, out = call("discover", "-c", path, "--json", "--estimate-only")
    assert code == 0 and json.loads(out)["estimate"]["cost_usd"] > 0
    code, out = call("discover", "-c", path, "--json", "--max-cost", "0.01")
    stopped = json.loads(out)
    assert code == 3 and "budget" in stopped["error"] and stopped["estimate"]["above_max_cost"]
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
    base = json.loads(call("ci", "--snapshot", "-c", path)[1])
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


def unpriced(tmp_path):
    path = tmp_path / "misroute.yaml"
    data = {
        "server": {"command": [sys.executable, "-m", "misroute.testing.fake_server"]},
        "models": [
            {"name": "astra", "provider": "openai-compatible", "model": "no-such", "base_url": "https://x.invalid"}
        ],
        "workdir": str(tmp_path / ".misroute"),
    }
    path.write_text(yaml.safe_dump(data))
    return str(path)


def test_unknown_price_blocks_paid_runs_and_estimates(tmp_path):
    for extra in (["--yes"], ["--max-cost", "1"], ["--estimate-only"]):
        code, out = call("discover", "-c", unpriced(tmp_path), "--json", *extra)
        assert code == 3, out
        assert "no price known for astra" in json.loads(out)["error"]


def test_max_cost_is_a_real_limit_on_every_command(config):
    path = config(price=1000)
    assert load(Opts("discover", max_cost=1.5), Path(path)).budget.limit == 1.5
    assert load(Opts("discover"), Path(path)).budget.limit == 5.0
    _, out = call("discover", "-c", path, "--json", "--estimate-only")
    assert json.loads(out)["estimate"]["upper_bound"] is False


def test_merged_commands_do_the_work_of_the_old_ones(config, tmp_path, monkeypatch):
    path = config()
    code, out = call("discover", "-c", path, "--json", "--yes", "--pair", "get_case_comments,get_case_events")
    found = json.loads(out)
    assert code == 0 and found["spots"] and found["probe"]["rows"][0]["pair"] == "get_case_comments|get_case_events"
    code, out = call("check", "-c", path, "--json", "--yes", "--pair", "get_case_comments,get_case_events")
    tested = json.loads(out)
    assert tested["command"] == "check" and tested["pairs"][0]["edits"]["clause"]
    assert tested["verdict"] in ("better", "no_difference", "insufficient_data", "worse")
    assert code == {"worse": 1, "insufficient_data": 2}.get(tested["verdict"], 0)
    code, out = call("check", "-c", path, "--json")
    assert code == 3 and "--pair" in json.loads(out)["error"]
    code, out = call("report", "-c", path, "--json", "--output", str(tmp_path / "r.html"))
    assert code == 0 and (tmp_path / "r.html").exists() and json.loads(out)["report"]
    site = tmp_path / "site"
    code, _ = call("report", "--site", f"demo={tmp_path / '.misroute'}", "--output", str(site))
    assert code == 0 and (site / "index.html").exists()
    assert call("serve", "nothing", "-c", path)[0] == 3


def test_old_command_names_say_where_they_went(config):
    path = config()
    for old, new in (("fix", "check --pair"), ("explain", "report --pair"), ("init", "quick --config-only")):
        code, out = call(old, "--json", "--pair", "a,b")
        assert code == 3 and new in json.loads(out)["error"], old
    assert call("probe")[0] == 3 and call("guard", "serve")[0] == 3
    code, out = call("catalog", "-c", path)
    assert code == 0 and json.loads(out)["tools"]


def test_quick_writes_the_demo_config_and_stops(tmp_path):
    path = tmp_path / "misroute.yaml"
    code, out = call("quick", "--demo", "--config-only", "-c", str(path), "--json")
    assert code == 0 and json.loads(out)["config"] == str(path)
    assert yaml.safe_load(path.read_text())["models"][0]["provider"] == "mock"
    assert call("quick", "--config-only", "-c", str(path), "--json")[0] == 3
