import asyncio
import json
from importlib import resources
from pathlib import Path
from typing import Any
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from misroute.guard.wrapper import approve
from misroute.models.base import BudgetExceeded
from misroute.project import Project
from misroute.report.html import html

PHASES = ("lint", "discover", "probe", "fix")


class Runner:
    def __init__(self, config: Path):
        self.config = config
        self.task: asyncio.Task | None = None
        self.state: dict[str, Any] = {"phase": None, "done": 0, "total": 0, "error": None, "spent": 0.0}

    def project(self) -> Project:
        return Project.from_yaml(self.config)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def estimate(self, body: dict) -> dict:
        proj = self.project()
        if body["phase"] == "lint":
            return {"calls": 0, "cost": 0.0}
        calls, cost = await proj.cost(body["phase"], body["models"], body["modes"], body["top"], body["trials"])
        return {"calls": calls, "cost": cost}

    def start(self, body: dict, calls: int) -> None:
        proj = self.project()
        self.state = {"phase": body["phase"], "done": 0, "total": calls, "error": None, "spent": 0.0}

        def tick(n: int) -> None:
            self.state["done"] += n
            self.state["spent"] = proj.budget.spent

        proj.progress = tick
        phase, ms, md = body["phase"], body["models"], body["modes"]
        coro = {
            "lint": lambda: proj.lint(draft=True),
            "discover": lambda: proj.discover(ms, md),
            "probe": lambda: proj.probe(body["top"], None, ms, md),
            "fix": lambda: proj.fix(body["top"], None, ms, md, body["trials"]),
        }[phase]()

        async def run() -> None:
            try:
                await coro
            except BudgetExceeded as e:
                self.state["error"] = str(e)
            except Exception as e:
                self.state["error"] = f"{type(e).__name__}: {e}"
            self.state["spent"] = proj.budget.spent

        self.task = asyncio.create_task(run())


def guard_entries(log: Path) -> dict[str, Any]:
    if not log.exists():
        return {"held": [], "events": []}
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    approved_path = log.with_name("approved.json")
    approved = set(json.loads(approved_path.read_text())) if approved_path.exists() else set()
    called = {r["id"] for r in rows if r.get("type") == "call"}
    held = {r["id"]: r for r in rows if r.get("type") == "held" and r["id"] not in called}
    for r in held.values():
        r["approved"] = r["id"] in approved
    return {"held": list(held.values()), "events": [r for r in rows if r.get("type") == "event"][-200:]}


def create(config: Path) -> Starlette:
    runner = Runner(config)
    page = resources.files("misroute.ui").joinpath("index.html").read_text(encoding="utf-8")

    def guarded(request: Request) -> Response | None:
        if request.headers.get("x-misroute") != "1":
            return JSONResponse({"error": "missing header"}, status_code=403)
        return None

    async def body_of(request: Request) -> dict:
        b = await request.json()
        cfg = runner.project().cfg
        phase = b.get("phase")
        if phase not in PHASES:
            raise ValueError("unknown phase")
        return {
            "phase": phase,
            "models": b.get("models") or cfg.tested(),
            "modes": b.get("modes") or list(cfg.modes),
            "top": int(b.get("top") or 3),
            "trials": int(b.get("trials") or 96),
        }

    async def index(request: Request) -> Response:
        return HTMLResponse(page)

    async def report(request: Request) -> Response:
        return HTMLResponse(html(runner.project().store))

    async def state(request: Request) -> Response:
        cfg = runner.project().cfg
        return JSONResponse(
            {
                "running": runner.running,
                **runner.state,
                "models": [
                    {"name": m.name, "provider": m.provider, "tested": m.name in cfg.tested()} for m in cfg.models
                ],
                "modes": list(cfg.modes),
                "budget": cfg.budget_usd,
            }
        )

    async def estimate(request: Request) -> Response:
        if bad := guarded(request):
            return bad
        return JSONResponse(await runner.estimate(await body_of(request)))

    async def run(request: Request) -> Response:
        if bad := guarded(request):
            return bad
        if runner.running:
            return JSONResponse({"error": "a run is already going"}, status_code=409)
        body = await body_of(request)
        est = await runner.estimate(body)
        runner.start(body, est["calls"])
        return JSONResponse({"started": body["phase"], **est})

    async def guard(request: Request) -> Response:
        return JSONResponse(guard_entries(Path(runner.project().cfg.guard.log)))

    async def guard_approve(request: Request) -> Response:
        if bad := guarded(request):
            return bad
        cid = (await request.json())["id"]
        approve(runner.project().cfg.guard.log, cid)
        return JSONResponse({"approved": cid})

    return Starlette(
        routes=[
            Route("/", index),
            Route("/report", report),
            Route("/api/state", state),
            Route("/api/estimate", estimate, methods=["POST"]),
            Route("/api/run", run, methods=["POST"]),
            Route("/api/guard", guard),
            Route("/api/guard/approve", guard_approve, methods=["POST"]),
        ]
    )


def serve(config: Path, port: int = 8765) -> None:
    import uvicorn

    uvicorn.run(create(config), host="127.0.0.1", port=port, log_level="warning")
