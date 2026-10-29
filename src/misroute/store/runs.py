import json
import time
from pathlib import Path
from typing import Any
import duckdb
from misroute.trial import Trial

SCHEMA = """
create table if not exists runs (run_id varchar, phase varchar, started double, catalog varchar, info json);
create table if not exists trials (
    run_id varchar, phase varchar, model varchar, mode varchar, condition varchar, pair varchar, target varchar,
    request_id varchar, kind varchar, slot varchar, ord integer, rep integer, chosen varchar, calls json,
    hedge boolean, wrong boolean, failure varchar, probs json, text varchar, detail varchar
);
"""


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self.db_path = self.root / "misroute.duckdb"
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(SCHEMA)

    def run_id(self, phase: str, catalog: str) -> str:
        return f"{phase}-{time.strftime('%Y%m%d-%H%M%S')}-{catalog[:8]}"

    def save(self, run_id: str, phase: str, catalog: str, trials: list[Trial], info: dict[str, Any]) -> None:
        rows = [
            (
                run_id,
                phase,
                t.model,
                t.mode,
                t.condition,
                t.pair,
                t.target,
                t.request_id,
                t.kind,
                t.slot,
                t.order,
                t.rep,
                t.chosen,
                json.dumps(t.calls),
                t.hedge,
                t.wrong,
                t.failure,
                json.dumps(t.probs),
                t.text,
                t.detail,
            )
            for t in trials
        ]
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(
                "insert into runs values (?, ?, ?, ?, ?)", [run_id, phase, time.time(), catalog, json.dumps(info)]
            )
            if rows:
                con.executemany(f"insert into trials values ({', '.join('?' * 20)})", rows)
        with (self.root / "runs" / f"{run_id}.jsonl").open("w", encoding="utf-8") as f:
            for t in trials:
                f.write(json.dumps({"run_id": run_id, **t.to_json()}) + "\n")

    def write(self, name: str, data: Any) -> Path:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
        return path

    def read(self, name: str) -> Any:
        path = self.root / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
