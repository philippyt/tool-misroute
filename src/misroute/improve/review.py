import time
from pathlib import Path
from typing import Any
from misroute.improve import export
from misroute.server import openapi

DECISIONS = ("pending", "accept", "reject", "edit")


def reviewed(entries: list[dict]) -> list[dict]:
    """Entries as a person decided: rejected ones go back to the original, edits replace the proposal and count as
    untested, exclusions carry over. Entries without a decision keep the proposal and show as pending."""
    out = []
    for e in entries:
        r = e.get("review") or {}
        kind = r.get("decision", "pending")
        row = {**e, "exclude": bool(r.get("exclude")), "review_state": kind}
        if kind == "reject":
            row["description"] = e["original"]
        elif kind == "edit" and r.get("description"):
            row["description"] = r["description"]
            row["status"], row["reason"] = "edited", "edited by a reviewer, untested, check it with misroute check"
        out.append(row)
    return out


def save(store, decisions: dict[str, dict[str, Any]], reviewer: str = "") -> dict[str, Any]:
    """Record review decisions on the stored improve result and rewrite its files. Nothing touches the source."""
    res = store.read("improve")
    if not res:
        raise ValueError("no improve results yet, run misroute improve first")
    names = {e["tool"] for e in res["entries"]}
    unknown = sorted(set(decisions) - names)
    if unknown:
        raise ValueError(f"unknown tools {', '.join(unknown)}")
    for e in res["entries"]:
        d = decisions.get(e["tool"])
        if d is None:
            continue
        kind = d.get("decision", "pending")
        if kind not in DECISIONS:
            raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
        text = " ".join(str(d.get("description") or "").split())
        if kind == "edit" and not text:
            raise ValueError(f"{e['tool']}: an edit needs a description")
        e["review"] = {"decision": kind, "exclude": bool(d.get("exclude")), "at": time.time(), "by": reviewer}
        if kind == "edit":
            e["review"]["description"] = text
    files = res.get("files") or {}
    if files.get("descriptions"):
        out = Path(files["descriptions"]).parent
        spec = res["source"] if files.get("overlay") else None
        doc = openapi.load(spec) if spec else None
        res["files"] = export.write(out, reviewed(res["entries"]), spec, doc, "server" in files)
    counts: dict[str, int] = {}
    for e in res["entries"]:
        k = (e.get("review") or {}).get("decision", "pending")
        counts[k] = counts.get(k, 0) + 1
    res["review"] = {"counts": counts, "at": time.time()}
    store.write("improve", res)
    return {"review": res["review"], "files": res.get("files", {})}
