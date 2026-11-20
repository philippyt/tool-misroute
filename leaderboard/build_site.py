#!/usr/bin/env python3
"""Build a self-contained monochrome editorial HTML + inline SVG leaderboard from leaderboard.json.

Usage:
    uv run python leaderboard/build_site.py leaderboard/results/surface/leaderboard.json
"""

from __future__ import annotations
import argparse
import html
import json
from pathlib import Path
from typing import Any
from misroute.store.manifest import score_audit

CSS = """
:root { --bg: #000; --fg: #fff; --muted: #9a9a9a; --rule: #242424; --accent: #e04f2c; }
* { border-radius: 0; box-sizing: border-box; }
body {
  background: var(--bg); color: var(--fg);
  font: 15px/1.55 "Familjen Grotesk", system-ui, sans-serif;
  margin: 0 auto; max-width: 1080px; padding: 36px 18px 96px;
}
h1 { font-size: 30px; font-weight: 700; margin: 0 0 6px; letter-spacing: -0.02em; }
h2 { font-size: 19px; font-weight: 700; margin: 40px 0 10px; padding-top: 18px; border-top: 1px solid var(--rule); }
p { margin: 0 0 10px; }
.muted, .sub { color: var(--muted); font-size: 13px; }
.write { color: var(--accent); font-weight: 600; }
code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.vbar { width: 100%; height: 26px; display: block; border: 1px solid var(--rule); margin: 12px 0 8px; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 13px; margin-bottom: 10px; }
.leg i {
  display: inline-block; width: 10px; height: 10px;
  margin-right: 6px; vertical-align: -1px; border: 1px solid #333;
}
.controls { display: flex; flex-wrap: wrap; gap: 18px; margin: 12px 0; }
.tbtn {
  background: none; border: 0; border-bottom: 2px solid transparent;
  color: var(--muted); font: inherit; font-size: 13px; font-weight: 600;
  padding: 4px 2px; cursor: pointer; margin-right: 10px;
}
.tbtn.on { color: var(--fg); border-bottom-color: var(--accent); }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin-top: 8px; }
th, td {
  padding: 8px 14px 8px 0; text-align: left;
  border-bottom: 1px solid var(--rule); font-variant-numeric: tabular-nums; vertical-align: top;
}
th { font-weight: 700; font-size: 13px; color: var(--muted); white-space: nowrap; }
table.heat th, table.heat td { padding: 6px 8px; }
td.cell {
  text-align: right; font-family: ui-monospace, monospace;
  font-size: 12.5px; border: 1px solid #141414; min-width: 58px;
}
td.cell.empty { color: #444; text-align: center; }
tr.ctrl td:first-child { border-left: 2px solid #555; padding-left: 8px; }
.mview { display: none; }
.mview.active { display: block; }
details { margin-top: 4px; }
summary { cursor: pointer; color: var(--muted); font-size: 12.5px; }
details ul { margin: 6px 0 0; padding-left: 16px; font-size: 12.5px; }
""".strip()

FONT_LINK = (
    '<link href="https://fonts.googleapis.com/css2?family=Familjen+Grotesk:wght@400;500;600;700&display=swap" '
    'rel="stylesheet">'
)


def _esc(val: Any) -> str:
    return html.escape(str(val or ""))


def _pct(val: float | None) -> str:
    if val is None:
        return "n/a"
    return f"{val * 100:.1f}%"


def _short_model(name: str) -> str:
    return name.split("/")[-1]


def _variance_bar_svg(logit: dict[str, Any]) -> str:
    factors = logit.get("factors") or {}
    if not factors:
        return '<p class="muted">Not enough variance across factors to decompose.</p>'
    order = [
        ("tool", "Tool / description", "#e04f2c"),
        ("model", "Model", "#ffffff"),
        ("mode", "Mode (JSON vs code)", "#999999"),
        ("order", "Catalog order", "#666666"),
        ("same_gen_family", "Generator family match", "#444444"),
    ]
    rows = []
    for key, label, color in order:
        if key in factors:
            share = float(factors[key].get("share_of_unique_deviance", 0.0))
            rows.append((key, label, color, share))
    if not rows or sum(r[3] for r in rows) <= 0:
        return '<p class="muted">Deviance decomposition unavailable for this run.</p>'

    width = 920
    bar_h = 26
    x = 0.0
    rects = []
    for _, _label, color, share in rows:
        w = max(share * width, 0.0)
        if w > 0.5:
            rects.append(f'<rect x="{x:.1f}" y="0" width="{w:.1f}" height="{bar_h}" fill="{color}" />')
            x += w
    legend_items = []
    for _, label, color, share in rows:
        pct_str = f"{share * 100:.1f}%"
        legend_items.append(
            f'<span class="leg"><i style="background:{color}"></i>{_esc(label)}: <b>{pct_str}</b></span>'
        )
    pseudo = float(logit.get("pseudo_r2", 0.0))
    clusters = int(logit.get("clusters", 0))
    return (
        f'<svg class="vbar" viewBox="0 0 {width} {bar_h}" preserveAspectRatio="none" role="img" '
        f'aria-label="Deviance decomposition bar">{"".join(rects)}</svg>'
        f'<div class="legend">{"".join(legend_items)}'
        f'<span class="leg muted">McFadden R&sup2; {pseudo:.3f} across {clusters} templates</span></div>'
    )


def _forest_svg(rate: float, ci: list[float], max_x: float = 0.5) -> str:
    w, h = 150, 18
    scale = max(max_x, 0.1)
    lo = max(0.0, min(ci[0], scale)) / scale * (w - 12) + 6
    hi = max(0.0, min(ci[1], scale)) / scale * (w - 12) + 6
    pt = max(0.0, min(rate, scale)) / scale * (w - 12) + 6
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-label="{rate * 100:.1f}%">'
        f'<line x1="6" y1="9" x2="{w - 6}" y2="9" stroke="#262626" stroke-width="1" />'
        f'<line x1="{lo:.1f}" y1="9" x2="{hi:.1f}" y2="9" stroke="#ffffff" stroke-width="2" />'
        f'<line x1="{lo:.1f}" y1="5" x2="{lo:.1f}" y2="13" stroke="#ffffff" stroke-width="1.2" />'
        f'<line x1="{hi:.1f}" y1="5" x2="{hi:.1f}" y2="13" stroke="#ffffff" stroke-width="1.2" />'
        f'<circle cx="{pt:.1f}" cy="9" r="3" fill="#e04f2c" />'
        f"</svg>"
    )


def _heat_color(rate: float) -> str:
    r = min(max(rate, 0.0), 0.5) / 0.5
    if r <= 0.01:
        return "#0c0c0c"
    red = int(20 + r * 204)
    green = int(20 + r * 59)
    blue = int(20 + r * 24)
    return f"rgb({red},{green},{blue})"


def build_html(board: dict[str, Any], audit_summary: dict[str, Any] | None = None) -> str:
    models = board.get("models") or []
    servers = board.get("servers") or []
    matrix = board.get("matrix") or {}
    pairs = board.get("cross_family_pairs") or []
    logit = board.get("logit") or {}
    modes = board.get("modes") or ["json"]

    def _srv_avg(srv_name: str) -> float:
        vals = []
        for m in models:
            for mode in modes:
                cell = (matrix.get(srv_name) or {}).get(m["model"], {}).get(mode) or {}
                if cell.get("trials"):
                    vals.append(float(cell.get("wrong_rate", 0.0)))
        return sum(vals) / len(vals) if vals else 0.0

    ordered_servers = sorted(
        servers,
        key=lambda s: (s.get("category") == "control", -_srv_avg(s["server"]), s["server"]),
    )

    max_wrong = max([float(m["other_family"].get("wrong_ci95", [0, 0.2])[1]) for m in models] or [0.25])
    max_wrong = min(max(max_wrong, 0.25), 1.0)

    model_rows_html = []
    same_gen_term = (logit.get("terms") or {}).get("same_gen_family")
    for m in models:
        o = m.get("other_family") or {}
        own = m.get("own_family") or {}
        wr = float(o.get("wrong_rate", 0.0))
        wci = o.get("wrong_ci95") or [wr, wr]
        term = m.get("logit_term")
        or_str = f"{term['odds_ratio']:.2f}" if term else "1.00 (ref)"
        delta = (
            f"{(float(own.get('error_rate', 0.0)) - float(o.get('error_rate', 0.0))) * 100:+.1f} pp"
            if own.get("trials")
            else "n/a"
        )
        model_rows_html.append(
            "<tr>"
            f"<td><strong>{_esc(_short_model(m['model']))}</strong><div class='sub'>{_esc(m['family'])}</div></td>"
            f"<td>{_pct(wr)} <span class='sub'>[{wci[0] * 100:.0f}, {wci[1] * 100:.0f}]</span></td>"
            f"<td>{_forest_svg(wr, wci, max_wrong)}</td>"
            f"<td>{_pct(o.get('hedge_rate'))}</td>"
            f"<td class='write'>{_pct(o.get('write_hedge_rate'))}</td>"
            f"<td>{_pct(o.get('bad_args_rate'))}</td>"
            f"<td>{or_str}</td>"
            f"<td>{delta}</td>"
            "</tr>"
        )

    matrix_blocks = []
    metrics = [("wrong_rate", "Wrong tool"), ("hedge_rate", "Hedged"), ("write_hedge_rate", "Write-hedged")]
    for mode in modes:
        for mkey, _mlabel in metrics:
            active = " active" if (mode == modes[0] and mkey == "wrong_rate") else ""
            hdr = "".join(f"<th>{_esc(_short_model(m['model']))}</th>" for m in models)
            body_rows = []
            for s in ordered_servers:
                srv = s["server"]
                cls = " class='ctrl'" if s.get("category") == "control" else ""
                cells = []
                for m in models:
                    c = (matrix.get(srv) or {}).get(m["model"], {}).get(mode) or {}
                    if c.get("trials"):
                        v = float(c.get(mkey, 0.0))
                        bg = _heat_color(v)
                        tip = f"{_esc(srv)} / {_esc(m['model'])}: {v * 100:.1f}% ({c['trials']} trials)"
                        cells.append(f'<td class="cell" style="background:{bg}" title="{tip}">{v * 100:.0f}%</td>')
                    else:
                        cells.append('<td class="cell empty">-</td>')
                tag = " <span class='sub'>control</span>" if s.get("category") == "control" else ""
                body_rows.append(
                    f"<tr{cls}><td><strong>{_esc(srv)}</strong>{tag}"
                    f"<div class='sub'>{s.get('tool_count', 0)} tools ({s.get('write_count', 0)}w)</div></td>"
                    f"{''.join(cells)}</tr>"
                )
            matrix_blocks.append(
                f'<div class="mview{active}" data-mode="{_esc(mode)}" data-metric="{_esc(mkey)}">'
                f'<div class="scroll"><table class="heat"><tr><th>Server</th>{hdr}</tr>'
                f"{''.join(body_rows)}</table></div></div>"
            )

    pair_rows_html = []
    for p in pairs[:30]:
        wcls = " write" if p.get("write") else ""
        ci = p.get("ci95") or [0.0, 0.0]
        dirs = ", ".join(f"{k} ({v})" for k, v in (p.get("directions") or {}).items())
        ex_list = []
        for ex in p.get("examples") or []:
            called = _esc(", ".join(ex.get("called") or []))
            ex_list.append(
                f"<li><code>{_esc(_short_model(ex['model']))} [{_esc(ex['mode'])}]</code> "
                f'asked <em>"{_esc(ex["text"])}"</em> &rarr; called <code>{called}</code> '
                f"(target <code>{_esc(ex['target'])}</code>)</li>"
            )
        details = f"<details><summary>transcripts</summary><ul>{''.join(ex_list)}</ul></details>" if ex_list else ""
        pair_rows_html.append(
            "<tr>"
            f"<td>{_esc(p['server'])}</td>"
            f"<td class='{wcls}'>{_esc(p['a'])} &harr; {_esc(p['b'])}{details}</td>"
            f"<td><strong>{p['families_confused']}/{p['total_families']}</strong>"
            f"<div class='sub'>{_esc(', '.join(p.get('families') or []))}</div></td>"
            f"<td>{_pct(p['error_rate'])} <span class='sub'>[{ci[0] * 100:.0f}, {ci[1] * 100:.0f}]</span></td>"
            f"<td>{p['wrong']} wrong / {p['hedge']} hedge</td>"
            f"<td class='sub'>{_esc(dirs)}</td>"
            "</tr>"
        )

    gen_bias_note = "Not enough cross-family generator variation in this run."
    if same_gen_term:
        gen_bias_note = (
            f"Generator-family match effect (clustered Firth logit): "
            f"OR {same_gen_term['odds_ratio']:.2f}, "
            f"AME {same_gen_term.get('ame', 0.0) * 100:+.1f} pp, "
            f"p = {same_gen_term['p']:.3g}. All headline rates below exclude each model's own generator family."
        )

    if audit_summary and audit_summary.get("labeled"):
        ap = audit_summary.get("audited_precision")
        ap_ci = audit_summary.get("audited_precision_ci95") or [0, 0]
        le = audit_summary.get("label_error_rate")
        le_ci = audit_summary.get("label_error_rate_ci95") or [0, 0]
        audit_html = (
            f"<p>Human audit ({audit_summary['labeled']} labeled of {audit_summary['total_sampled']} sampled): "
            f"audited precision <strong>{_pct(ap)}</strong> [{ap_ci[0] * 100:.0f}, {ap_ci[1] * 100:.0f}], "
            f"label error rate <strong>{_pct(le)}</strong> [{le_ci[0] * 100:.0f}, {le_ci[1] * 100:.0f}].</p>"
        )
    else:
        audit_html = (
            "<p class='muted'>Human audit queue exported to <code>audit_sample.jsonl</code> (pending manual review). "
            "Synthetic rates are a routing diagnostic until audited.</p>"
        )

    srv_manifest_rows = "".join(
        f"<tr><td>{_esc(s['server'])}</td><td><code>{_esc(s.get('package'))}</code></td>"
        f"<td><code>{_esc(s.get('source_ref'))}</code></td><td>{s.get('tool_count', 0)}</td>"
        f"<td>{_pct((s.get('validator_agreement') or {}).get('unanimous_rate'))}</td></tr>"
        for s in ordered_servers
    )

    mode_btns = "".join(
        f'<button type="button" class="tbtn{" on" if i == 0 else ""}" '
        f'data-set-mode="{_esc(m)}">{_esc(m.upper())}</button>'
        for i, m in enumerate(modes)
    )
    metric_btns = "".join(
        f'<button type="button" class="tbtn{" on" if i == 0 else ""}" data-set-metric="{_esc(k)}">{_esc(lbl)}</button>'
        for i, (k, lbl) in enumerate(metrics)
    )

    built_at = _esc(str(board.get("generated_at", ""))[:19])
    ver = _esc(board.get("misroute_version"))
    spent = float(board.get("total_spent_usd", 0.0))
    empty_pairs = "<tr><td colspan='6' class='muted'>No confused pairs recorded.</td></tr>"
    pairs_body = "".join(pair_rows_html) if pair_rows_html else empty_pairs

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>misroute MCP tool routing leaderboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
{FONT_LINK}
<style>
{CSS}
</style>
</head>
<body>
<h1>misroute MCP tool routing leaderboard</h1>
<p class="muted">
  Cross-family tool confusion across {len(servers)} frozen official MCP servers and {len(models)} models.
  Built {built_at}Z with misroute {ver} (spend ${spent:.2f}).
</p>

<h2>1. Description vs. model variance decomposition</h2>
<p>
  Share of explained routing-error deviance from clustered Firth logistic regression
  (<code>error ~ tool + model + mode + order + same_gen_family</code>):
</p>
{_variance_bar_svg(logit)}
<p class="muted">{_esc(gen_bias_note)}</p>

<h2>2. Server &times; model confusion matrix (leave-own-family-out)</h2>
<p class="muted">
  Horizontal bands across all columns show description-level confusion shared across model families;
  vertical bands show model-specific errors.
</p>
<div class="controls">
  <div><span class="muted">Mode:</span> {mode_btns}</div>
  <div><span class="muted">Metric:</span> {metric_btns}</div>
</div>
{"".join(matrix_blocks)}

<h2>3. Model summary (leave-own-family-out)</h2>
<div class="scroll"><table>
  <tr>
    <th>Model</th><th>Wrong tool (95% CI)</th><th>Forest plot</th><th>Hedged</th>
    <th>Write-hedged</th><th>Bad args</th><th>Adj. OR</th><th>Own-family &Delta;</th>
  </tr>
  {"".join(model_rows_html)}
</table></div>

<h2>4. Cross-family confused tool pairs</h2>
<p class="muted">
  Ranked by distinct model families tripped on leave-own-family-out requests.
  Write tools highlighted in vermilion.
</p>
<div class="scroll"><table>
  <tr>
    <th>Server</th><th>Confused pair</th><th>Families</th>
    <th>Error rate (95% CI)</th><th>Breakdown</th><th>Direction</th>
  </tr>
  {pairs_body}
</table></div>

<h2>5. Reproducibility manifest &amp; human audit</h2>
{audit_html}
<div class="scroll"><table>
  <tr><th>Server</th><th>Package</th><th>Pinned ref</th><th>Tools</th><th>Validator agreement</th></tr>
  {srv_manifest_rows}
</table></div>

<script>
(function() {{
  let curMode = {json.dumps(modes[0])}, curMetric = "wrong_rate";
  function sync() {{
    document.querySelectorAll(".mview").forEach(el => {{
      el.classList.toggle("active", el.dataset.mode === curMode && el.dataset.metric === curMetric);
    }});
    document.querySelectorAll("[data-set-mode]").forEach(b => {{
      b.classList.toggle("on", b.dataset.setMode === curMode);
    }});
    document.querySelectorAll("[data-set-metric]").forEach(b => {{
      b.classList.toggle("on", b.dataset.setMetric === curMetric);
    }});
  }}
  document.querySelectorAll("[data-set-mode]").forEach(b => {{
    b.addEventListener("click", () => {{ curMode = b.dataset.setMode; sync(); }});
  }});
  document.querySelectorAll("[data-set-metric]").forEach(b => {{
    b.addEventListener("click", () => {{ curMetric = b.dataset.setMetric; sync(); }});
  }});
}})();
</script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Build static HTML leaderboard from leaderboard.json.")
    parser.add_argument("leaderboard_json", help="Path to leaderboard.json")
    parser.add_argument("--audit", help="Optional path to labeled audit_sample.jsonl")
    parser.add_argument("--out", default="leaderboard/site/index.html")
    args = parser.parse_args()

    board = json.loads(Path(args.leaderboard_json).read_text(encoding="utf-8"))
    audit_summary = None
    audit_path = Path(args.audit) if args.audit else Path(args.leaderboard_json).parent / "audit_sample.jsonl"
    if audit_path.is_file():
        rows = [json.loads(ln) for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        audit_summary = score_audit(rows)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_html(board, audit_summary), encoding="utf-8")
    print(f"Built leaderboard site: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
