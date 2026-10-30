import asyncio
from typing import Any
from misroute.project import Project


def check_routes(
    tools: list[Any],
    model: str | list[str],
    modes: tuple[str, ...] = ("json",),
    max_cost: float = 1.0,
    root: str = ".misroute",
    **settings: Any,
) -> dict[str, Any]:
    """Run a small capped discover on tools held in Python. Repeat runs hit the cache and cost nothing."""
    defaults = {"requests_per_tool": 3, "rotations": 2, "repeats": 1, "budget_usd": max_cost, "modes": list(modes)}
    proj = Project.from_tools(tools, model, root=root, **{**defaults, **settings})
    return asyncio.run(proj.discover())


def assert_routes(tools: list[Any], model: str | list[str], min_accuracy: float = 0.95, **kw: Any) -> dict[str, Any]:
    """Fail with the confused pairs when any model and mode routes fewer requests correctly than min_accuracy."""
    res = check_routes(tools, model, **kw)
    low = [c for c in res["cells"] if c["accuracy"] < min_accuracy]
    if low:
        cells = ", ".join(f"{c['model']} {c['mode']} {c['accuracy']:.0%}" for c in low)
        pairs = "; ".join(f"{s['a']} and {s['b']} {s['score']:.2f}" for s in res["spots"][:5])
        raise AssertionError(f"routing accuracy below {min_accuracy:.0%}: {cells}. Most confused: {pairs}")
    return res
