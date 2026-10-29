from misroute.lint.descriptions import classify
from misroute.modes.stubs import stub
from misroute.server.catalog import Tool


def test_write_detection():
    assert Tool("update_case").is_write
    assert Tool("createIssue").is_write
    assert not Tool("get_case").is_write
    assert not Tool("delete_thing", read_only=True).is_write
    assert Tool("fetch", destructive=True).is_write


def test_hash_changes_with_description():
    assert Tool("a", "x").hash != Tool("a", "y").hash


def test_lint():
    assert classify(Tool("get_doc")) == "missing"
    assert classify(Tool("get_doc", "GET /api/docs/{id}")) == "path only"
    assert classify(Tool("get_doc", "get doc")) == "name only"
    assert classify(Tool("get_doc", "Returns the stored document with its metadata.")) is None


def test_stub():
    t = Tool(
        "find", "Find things.", {"properties": {"q": {"type": "string"}, "n": {"type": "integer"}}, "required": ["q"]}
    )
    assert stub(t).startswith("async def find(q: str, n: int | None = None) -> dict:")
