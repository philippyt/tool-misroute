# Using misroute from an agent

misroute tells you whether a model picks the right tool on an MCP server, and whether a change to a tool description helps. Use it when you edit tool names or descriptions.

## Rules

- misroute never edits tools. You propose a description, a person reviews and applies it.
- Drafts from `lint --draft` and `draft_description` are suggestions for review, never apply them yourself.
- Always pass `--json` and either `--yes` or `--max-cost`. misroute does not prompt when there is no terminal, it exits with code 3 instead.
- Check the cost first with `--estimate-only` when you are unsure.

## When to run what

| Situation | Command | Cost |
|---|---|---|
| Before anything else | `misroute lint --json` | free |
| You changed one description | `misroute check --tool NAME --description "..." --json --max-cost 1` | a few hundred model calls |
| You want to find confused tools | `misroute discover --json --max-cost 5` | grows with the number of tools |
| A pull request changed descriptions | `misroute ci --changed --baseline tools.json --json --max-cost 2` | only changed tools and their neighbours |

## Check verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `better` | the proposed description beats the baseline on held out requests | 0 |
| `no_difference` | no significant difference, keep whichever reads clearer | 0 |
| `worse` | the proposed description makes routing worse | 1 |
| `insufficient_data` | too few discordant trials to decide, do not treat any number as an answer | 2 |

The baseline is a neutral rewrite of the current description of the same length. When the current description is missing or only a path, the baseline is lint's drafted description instead.

The verdict uses held out requests you never see results for. After 3 verdicts on the same tool the held out set is spent and `check` returns `insufficient_data` until you pass `--refresh`, which draws a fresh set. Do not keep tweaking a description against the same set.

Every result includes `trials`, `held_out` and `cost_usd`. Report them with the verdict so a reviewer can tell a solid result from a thin one.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed |
| 1 | limits failed, or a checked description got worse |
| 2 | not enough data to decide |
| 3 | budget, cost cap, provider or config problem |

## MCP server

`misroute serve-mcp -c misroute.yaml` exposes `lint_descriptions`, `draft_description`, `estimate_check`, `check_description` and `get_report` as MCP tools. `check_description` refuses to run when its estimate is above `max_cost`.
