from dataclasses import dataclass, field
from typing import Any
import pydantic_monty as pm

LIMITS: pm.ResourceLimits = {"max_feed_duration_secs": 10, "max_memory": 256 * 1024 * 1024, "max_suspensions": 200}


@dataclass
class RunResult:
    output: Any = None
    stdout: str = ""
    error: str | None = None
    error_type: str | None = None
    prints: list[str] = field(default_factory=list)


class Sandbox:
    """One Monty worker pool, a fresh session per program."""

    def __init__(self, max_processes: int = 4):
        self.pool = pm.AsyncMonty(max_processes=max_processes, request_timeout=30)

    async def __aenter__(self) -> "Sandbox":
        await self.pool.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.pool.__aexit__(*exc)

    async def run(self, code: str, functions: dict[str, Any]) -> RunResult:
        res = RunResult()
        try:
            async with self.pool.checkout(limits=LIMITS) as session:
                res.output = await session.feed_run(
                    code, external_lookup=functions, print_callback=lambda _k, t: res.prints.append(t)
                )
        except pm.MontySyntaxError as e:
            res.error, res.error_type = str(e), "SyntaxError"
        except pm.MontyRuntimeError as e:
            res.error = e.display("type-msg")
            res.error_type = res.error.split(":", 1)[0]
        except (pm.MontyError, TimeoutError) as e:
            res.error, res.error_type = str(e), type(e).__name__
        res.stdout = "".join(res.prints)
        return res
