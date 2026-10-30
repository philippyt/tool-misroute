# Using misroute from an agent

misroute tells you whether a model picks the right tool on an MCP server or an OpenAPI spec, and whether a change to a tool description helps. Use it when you edit tool names or descriptions, or when you turn a Swagger file into MCP tools.

After `pip install misroute` or `uv tool install misroute`, `misroute guide` prints this file and the reference in llms.txt. The MCP server serves the same text as the `misroute://guide` resource.

## Rules

- misroute never edits tools. You propose a description, a person reviews and applies it.
- Drafts from `lint --draft`, `draft_description` and `improve` entries with status `draft` are untested suggestions. Say so when you show them.
- Always pass `--json` and either `--yes` or `--max-cost`. misroute does not prompt without a terminal, it exits with code 3 instead.
- Check the cost first with `--estimate-only` when you are unsure.
- Report `trials`, `held_out` and `cost_usd` with every verdict, and the template counts from `confirm.tests` with every improve proposal.
- `insufficient_data` is not an answer. Do not read anything into its rates.
- Do not tune a description against the same held out set. After 3 verdicts on a tool, ask the user before passing `--refresh`.

## Setup

misroute needs a target and a model. A target is an MCP URL, a local command, an OpenAPI or Swagger file, or a tool list in MCP, OpenAI or Anthropic format. Models use `provider:model` shorthand: `anthropic`, `foundry`, `bedrock`, `azure`, `openai`, `openrouter`, `ollama`, `vllm`, `lmstudio`. Keys come from the environment or a `.env` next to misroute.yaml. Prices are looked up automatically and local models are free.

Real requests are the strongest evidence. Their rates show in discover `by_kind.real` and in improve `confirm.rates[condition].real_error_rate`.

The model under test should not write or judge its own test requests. Set a helper model from another family: `--helper` on `improve`, or `generator`, `validator` and `writer` in misroute.yaml. Results carry `roles_warning` when a role shares a family with a tested model.

```yaml
server:
  openapi: api.json            # or url, command, tools
  include_tools: ["Orders_*"]  # optional
models:
  - foundry:claude-opus-5-5
generator: openai:gpt-5.6
validator: openai:gpt-5.6
writer: openai:gpt-5.6
request_source: spec           # write requests from the interface, not the descriptions
request_languages: [no, en]
domain: Work orders are planned jobs, incidents are reported faults.
real_requests: real.jsonl      # optional, lines of {"text": ..., "tool": name, operationId or "METHOD /path", "lang": ...}
```

## When to run what

| Situation | Command | Cost |
|---|---|---|
| Nothing is set up | `misroute quick <target> --json --max-cost 1 --no-open` | capped |
| Something does not connect | `misroute doctor --json` | one call per model |
| Before anything else | `misroute lint --json` | free |
| You want to know why a pair is confused | `misroute explain --pair a,b --json` | free |
| You changed one description | `misroute check --tool NAME --description "..." --json --max-cost 1` | a few hundred model calls |
| You want to find confused tools | `misroute discover --json --max-cost 5` | grows with the number of tools |
| You want better descriptions for a server or a Swagger file | `misroute improve <target> --model M --helper H --json --max-cost 5` | discover plus a race per confused group |
| A pull request changed descriptions | `misroute ci --changed --baseline tools.json --json --max-cost 2` | only changed tools and their neighbours |

## improve

```console
misroute improve api.json --model foundry:claude-opus-5-5 --helper openai:gpt-5.6 --languages no,en --fastmcp --json --max-cost 5
```

| Option | Meaning |
|---|---|
| target | OpenAPI or Swagger file, MCP URL, tool list or command. Without it, the server in misroute.yaml |
| `--model` | models under test, comma separated |
| `--helper` | model that writes and judges requests and writes candidates |
| `--languages` | languages to write requests in, results come per language |
| `--fastmcp` | OpenAPI only: also write a FastMCP server.py and confirm the results against it |
| `--out` | folder for the results, default `improve` |
| `--domain` | glossary for writing requests, text or a file |
| `--real-requests` | JSON lines of real user requests, the strongest evidence |
| `--requests-from` | `interface` (default) or `descriptions` |
| `--max-groups`, `--styles`, `--max-chars`, `--modes`, `--no-drafts` | tuning, rarely needed |

It audits the tools for free, runs discover, groups confused tools, writes candidates in the styles boundary, returns, example and minimal, drops candidates that claim what the interface lacks, races them on tuning requests and confirms original, baseline and winner on held out requests.

Each entry in `entries` has a `status`:

| Status | Meaning |
|---|---|
| `tested` | the winner beat the baseline on held out requests |
| `tested baseline` | only the baseline beat the original |
| `kept` | no tested change beat the original |
| `draft` | a weak tool outside any confused group, untested draft |
| `unchanged` | no confusion and no weak description found |

Files in `--out`: `descriptions.yaml` (tool name to description), and for OpenAPI `overlay.yaml` (per operationId, with hints, suggested exclusions and evidence), `overlay.openapi.yaml` (OpenAPI Overlay 1.0) and with `--fastmcp` `server.py`. A person reviews them, by hand or in the Improve tab of `misroute ui`, which shows each group's numbers, what the model picked on held out requests and every candidate, and saves Accept, Reject, Edit and Exclude decisions. Saving rewrites the files: rejected entries go back to the original, edits are marked untested, and each operation in overlay.yaml gets `review`. `exclude: true` hides an operation in server.py. Suggest `misroute ui` to the user when they should look over the results.

## Check verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `better` | the proposed description beats the baseline on held out requests | 0 |
| `no_difference` | no significant difference, keep whichever reads clearer | 0 |
| `worse` | the proposed description makes routing worse | 1 |
| `insufficient_data` | fewer than 6 request templates differ, no answer | 2 |

The baseline is a neutral rewrite of the current description of the same length, or lint's drafted description when the current one is missing or only a path. Trials from one request template fail together, so the test counts templates: an exact sign flip test on per template error differences, Holm corrected across pairs or groups. `diff` is the error rate change with a 95% interval in `ci`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed |
| 1 | limits failed, or a checked description got worse |
| 2 | not enough data to decide |
| 3 | budget, cost cap, provider or config problem |

## Output

Every `--json` output is one object with `schema_version` and `command`. Errors have `error` and `exit_code`. `misroute schema <command>` prints the JSON Schema. `--progress ndjson` streams progress events on stderr while stdout stays one JSON object. Results are also stored in `.misroute/`.

## MCP server

`misroute serve-mcp -c misroute.yaml` exposes:

| Tool | Cost |
|---|---|
| `lint_descriptions`, `get_report`, `explain_pair`, `job_status`, `cancel_job` | free |
| `draft_description`, `estimate_check` | one call or free |
| `check_description`, `suggest_fix` | paid, refuse above `max_cost` |
| `start_job` | paid, refuses above `max_cost`, `estimate_only` returns the cost |

Use `start_job` for discover, probe, fix and improve, since they outlast a tool call, then poll `job_status`. For improve, `args` takes `models`, `helper`, `languages`, `requests_from`, `domain`, `real_requests`, `max_groups`, `styles`, `max_chars`, `out`, `fastmcp` and `drafts`, with the same meaning as the CLI options.
