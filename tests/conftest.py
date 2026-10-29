import pytest
from misroute.config import Config
from misroute.project import Project
from misroute.testing.fake_server import app

MOCK = {
    "confuse": {"get_case_comments": {"get_case_events": 0.5}, "search_cases": {"list_cases": 0.4}},
    "hedge": {"update_case": ["update_case_meta"]},
    "fake": ["get_customer"],
}


@pytest.fixture
def cfg() -> Config:
    return Config.model_validate(
        {
            "server": {"command": ["unused"]},
            "models": [{"name": "mock", "provider": "mock", "options": MOCK}],
            "rotations": 2,
            "repeats": 2,
        }
    )


@pytest.fixture
def project(cfg, tmp_path) -> Project:
    return Project(cfg, server=app, root=tmp_path)
