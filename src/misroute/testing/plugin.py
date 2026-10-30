import pytest


@pytest.fixture
def misroute_routes(tmp_path_factory):
    """assert_routes with a shared cache for the session, so reruns are free."""
    from misroute.testing.routes import assert_routes

    root = tmp_path_factory.getbasetemp().parent / "misroute-cache"

    def check(tools, model, min_accuracy=0.95, **kw):
        return assert_routes(tools, model, min_accuracy, root=str(root), **kw)

    return check
