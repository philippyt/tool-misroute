import json
from typing import Any
import duckdb
from misroute.store.runs import Store


def explain(store: Store, a: str, b: str, limit: int = 5) -> dict[str, Any]:
    """Misrouted requests between two tools in both directions, from every stored run, with what the model did."""
    with duckdb.connect(str(store.db_path), read_only=True) as con:
        rows = con.execute(
            "select target, chosen, calls, hedge, text, detail, model, mode, phase, run_id from trials "
            "where target in (?, ?) and condition in ('original', 'baseline') order by run_id desc",
            [a, b],
        ).fetchall()
    out: dict[str, Any] = {"pair": f"{a}|{b}", "directions": {}}
    for src, dst in ((a, b), (b, a)):
        mine = [r for r in rows if r[0] == src]
        wrong = [r for r in mine if r[1] == dst and not r[3]]
        hedged = [r for r in mine if r[3] and dst in json.loads(r[2] or "[]")]
        seen, examples = set(), []
        for r in wrong + hedged:
            if r[4] in seen:
                continue
            seen.add(r[4])
            calls = json.loads(r[2] or "[]")
            examples.append(
                {
                    "request": r[4],
                    "called": calls,
                    "kind": "hedge" if r[3] else "wrong",
                    "model": r[6],
                    "mode": r[7],
                    "phase": r[8],
                    "model_output": (r[5] or "")[:400],
                }
            )
            if len(examples) >= limit:
                break
        out["directions"][f"{src}>{dst}"] = {
            "trials": len(mine),
            "wrong": len(wrong),
            "hedge": len(hedged),
            "examples": examples,
        }
    return out
