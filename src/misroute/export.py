import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

Result = tuple[str, str, str, str]
"""Rule id, level (error, warning or note), message and the file the result belongs to."""


def sarif(results: list[Result], path: str | Path) -> Path:
    rules = sorted({r[0] for r in results})
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "misroute", "rules": [{"id": r} for r in rules]}},
                "results": [
                    {
                        "ruleId": rule,
                        "level": level,
                        "message": {"text": message},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": Path(uri).as_posix()},
                                    "region": {"startLine": 1},
                                }
                            }
                        ],
                    }
                    for rule, level, message, uri in results
                ],
            }
        ],
    }
    out = Path(path)
    out.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return out


Case = tuple[str, str, str]
"""Test name, status (passed, failed or skipped) and message."""


def junit(suite: str, cases: list[Case], path: str | Path) -> Path:
    failed = sum(c[1] == "failed" for c in cases)
    skipped = sum(c[1] == "skipped" for c in cases)
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<testsuite name={quoteattr(suite)} tests="{len(cases)}" failures="{failed}" skipped="{skipped}">',
    ]
    for name, status, message in cases:
        lines.append(f"  <testcase classname={quoteattr(suite)} name={quoteattr(name)}>")
        if status == "failed":
            lines.append(f"    <failure message={quoteattr(message)}>{escape(message)}</failure>")
        elif status == "skipped":
            lines.append(f"    <skipped message={quoteattr(message)}/>")
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    out = Path(path)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
