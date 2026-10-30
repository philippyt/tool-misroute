import os
import sys
import yaml
from misroute.ui import setup


def form(**kw):
    base = {
        "server_kind": "command",
        "server_value": f"{sys.executable} -m misroute.testing.fake_server",
        "provider": "mock",
        "name": "main",
        "model": "m",
        "modes": ["json"],
        "budget_usd": "2",
    }
    return {**base, **kw}


async def test_setup_save_and_test(tmp_path, monkeypatch):
    monkeypatch.delenv(setup.KEY_VAR, raising=False)
    config = tmp_path / "misroute.yaml"
    assert setup.current(config) == {"configured": False}
    res = setup.save(config, form(api_key="secret", server_token="tok"))
    assert set(res["env"]) == {setup.KEY_VAR, setup.TOKEN_VAR}
    data = yaml.safe_load(config.read_text())
    assert "secret" not in config.read_text() and data["models"][0]["api_key_env"] == setup.KEY_VAR
    assert data["server"]["auth"] == f"bearer_env:{setup.TOKEN_VAR}"
    assert "MISROUTE_MODEL_KEY=secret" in (tmp_path / ".env").read_text()
    cur = setup.current(config)
    assert cur["configured"] and cur["has_key"] and cur["budget_usd"] == 2.0
    out = await setup.test(config, form())
    assert out["tools"]["count"] == 9 and "error" not in out["model"]
    os.environ.pop(setup.KEY_VAR, None)
    os.environ.pop(setup.TOKEN_VAR, None)
