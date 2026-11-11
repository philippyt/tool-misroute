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
- Discover rates come from written requests. Present them as a routing diagnostic, not as a production success rate, and name `flagged` pairs apart from `one_wording` pairs, which rest on a single request wording.
- When `accuracy` is low, check `decided` and `accuracy_decided` before calling it confusion: trials that ended before a choice was seen are errors that say nothing about which tool the model picks. Suggest a higher `max_turns` for those.
- When discover lists tools under `thin_interface`, say that requests for them may ask for more than the tool holds, and suggest output schemas or `request_source: description`.
- Report `bad_args_rate` next to the routing score, since a right tool name can hide a call the real tool would reject.
- Do not tune a description against the same held out set. Each verdict on a set multiplies its p value by the number of verdicts the set has given (`looks`), so repeated checks lose power. Ask the user before passing `--refresh` for a fresh set.

## Setup

misroute needs a target and a model. A target is an MCP URL, a local command, an OpenAPI or Swagger file, or a tool list in MCP, OpenAI or Anthropic format. Models use `provider:model` shorthand: `anthropic`, `foundry`, `bedrock`, `azure`, `openai`, `openrouter`, `ollama`, `vllm`, `lmstudio`. Keys come from the environment or a `.env` next to misroute.yaml. Prices are looked up automatically and local models are free. A model with no known price refuses paid runs and estimates, exit code 3: set `price_in` and `price_out`, or 0 when it is free.

A model that takes function tools only on the Responses API needs `options: {api: responses}` on a full model block. Set `reasoning_effort` in the same options, to the effort used in production. `misroute doctor` makes one tool call per model and names this fix.

`--max-cost` and `budget_usd` are limits on actual spend, and the lower one applies. Money is reserved before each call. The estimate is a guide, not an upper bound, and `upper_bound` is always false. An estimate above `--max-cost` does not refuse the run: the output carries `estimate.above_max_cost` and the run stops at the limit if spending gets there. Results carry `estimate` and `usage`, the measured calls, tokens and dollars per model. A budget stop exits with code 3, gives `spent` in the error, saves finished trials as a `partial` run and a rerun with more budget reuses them at no cost.

For a write request only the writes count, so a read before the write is not confusion. Tools count as writes from `readOnlyHint` and `destructiveHint`, or else a verb in the name. `effect_unknown` in lint and discover lists tools where neither says, which count as reads: ask the user to annotate them before trusting scores on them. The score is about tool names. Arguments are checked against the input schema and reported apart as `bad_args_rate` and `bad_args`, so say both when reporting on a generic tool that takes an operation name, and list its allowed operations in `domain`.

When a server is built so the agent reads a catalog or schema first, those calls are not confusion. `discover` lists likely entry points under `lookup_candidates`. Ask the user before adding them to `lookup_tools`, then calls to them are not scored as choices and a trial that only called lookups ends as `lookup_only`. Use `max_turns` of 2 or more with them, and `fixtures`, a file of recorded results per tool, so later turns see a realistic reply. `accuracy` is the first choice and `reached_rate` is whether the target was called at all.

Real requests are the strongest evidence. Their rates show in discover `by_kind.real` and in improve `confirm.rates[condition].real_error_rate`. `misroute requests collect` turns a guard log or a JSON lines trace into candidates with `reviewed: false`, which no run uses. Only a person confirms them, with `misroute requests review`. Never set `reviewed` yourself: what an agent called is not proof of what was right. `misroute requests status --json` gives `reviewed`, `pending` and `tools_without`.

To measure what production sees, set `system_prompt` to the agent's own prompt, as text or a file, and list every server the agent has connected under `servers`. Spots then carry `cross_server` for pairs that join two servers. Discover says `system_prompt: false` when it ran under the one line default.

`compare` gives `verdict` for the move from `from` to `to`: `better` means the new model makes fewer errors. Report it with `templates`, `diff`, `ci` and `cost_usd` per mode, list `tools` and `pairs.new`, and say when no request was real (`by_kind`). Exit codes follow check: 1 for `worse`, 2 for `insufficient_data`.

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
lookup_tools: [api_catalog]   # optional, entry points an agent is meant to call first
fixtures: fixtures.yaml       # optional, recorded results per tool for later turns
real_requests: real.jsonl      # optional, lines of {"text": ..., "tool": name, operationId or "METHOD /path", "lang": ...}
```

## When to run what

| Situation | Command | Cost |
|---|---|---|
| Nothing is set up | `misroute quick <target> --json --max-cost 1 --no-open` | capped |
| Something does not connect | `misroute doctor --json` | one call per model |
| Before anything else | `misroute lint --json` | free |
| You want to know why a pair is confused | `misroute report --pair a,b --json` | free |
| You want to press a risky pair harder | `misroute discover --pair a,b --json --max-cost 2` | a discover, free when cached, plus borrowed requests on the pair |
| You want a drafted boundary clause tested on a pair | `misroute check --pair a,b --json --max-cost 2` | three conditions on the pair |
| You changed one description | `misroute check --tool NAME --description "..." --json --max-cost 1` | a few hundred model calls |
| You want to find confused tools | `misroute discover --json --max-cost 5` | grows with the number of tools |
| The estimate looks off, or the model reasons | add `--pilot` to `discover` or `compare` | three trials per model and mode |
| A converted API has too many near identical tools | `misroute compare --design catalog.yaml --json --max-cost 5`, then ask the user to review `catalog.yaml` | free to propose, two runs of every request to test |
| The model is being changed | `misroute compare --from OLD --to NEW --json --max-cost 5` | a discover for each model, cached calls are free |
| There are logs of real use | `misroute requests collect --from FILE --json`, then ask the user to run `misroute requests review` | free |
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
| `--max-groups` | work on the first groups only, default every flagged group |
| `--styles`, `--max-chars`, `--modes`, `--no-drafts` | candidate styles, a length cut (none by default), modes, and whether to draft weak tools outside groups |

It audits the tools for free, runs discover, groups tools joined by pairs that meet the flag rule (6 errors in one model and mode, from the thesis), writes candidates in the styles boundary, returns, example and minimal, drops candidates that claim what the interface lacks, races every candidate in full on tuning requests and confirms original, baseline and winner on held out requests. The estimate covers discover; the later stages are priced once discover has found the groups, and the run stops before racing if they would pass the budget.

Each entry in `entries` has a `status`:

| Status | Meaning |
|---|---|
| `tested` | the winner beat a plain rewrite on held out requests and is not worse than the original. `beats_original` says whether it was also shown better than the original, report that separately |
| `tested baseline` | only the baseline beat the original |
| `kept` | no tested change beat the original |
| `draft` | a weak tool outside any confused group, untested draft |
| `unchanged` | no confusion and no weak description found |

A Swagger 2.0 source is converted to OpenAPI 3 on load, and `audit.notes` of kind `spec` list that and any repair. Other kinds to pass on to the user: `upload` (takes a file an agent cannot send), `binary` (returns only a file), `header` (a required header became a tool argument and may be a credential), `exclude`, `overlap`, `name` and `hint`.

Files in `--out`: `descriptions.yaml` (tool name to description), `openapi.json` when the source was converted or repaired, and for OpenAPI `overlay.yaml` (per operationId, with hints, suggested exclusions and evidence), `overlay.openapi.yaml` (OpenAPI Overlay 1.0) and with `--fastmcp` `server.py`. A person reviews them, by hand or in the Improve tab of `misroute serve ui`, which shows each group's numbers, what the model picked on held out requests and every candidate, and saves Accept, Reject, Edit and Exclude decisions. Saving rewrites the files: rejected entries go back to the original, edits are marked untested, and each operation in overlay.yaml gets `review`. `exclude: true` hides an operation in server.py. Suggest `misroute serve ui` to the user when they should look over the results.

## Numbers behind the results

Every threshold comes from a source or is derived. 0.05 and Holm are convention. The gate's 0.10, the flag rule of 6 errors and the 96 trial design (6 requests, 4 slots, both orders) come from the thesis. A flag also needs two request templates, the smallest count that is not one wording repeated, and a spot's interval counts templates. The minimum number of differing templates, and of pairs for the across pairs test, is the smallest count at which an exact sign flip test can reach 0.05. Monte Carlo draws are sized so the standard error at alpha is a tenth of alpha, and integer sign flip tests are exact for any size. FastMCP's 56 character name limit is read from its source. The rest are settings you choose: budgets, `max_tokens` (none by default, cut replies are retried with twice the tokens they used), guard windows and the pytest `min_accuracy`.

## Commands

There are twelve: `quick`, `doctor`, `lint`, `discover`, `check`, `improve`, `compare`, `ci`, `report`, `requests`, `serve` and `guide`. Older names were folded in and answer with the new form, exit code 3: `init` is `quick --config-only`, `probe` is `discover --probe`, `fix` is `check --pair`, `explain` is `report --pair`, `site` is `report --site`, `schema` is `guide --schema`, `ui` and `guard` are `serve ui` and `serve guard`. `serve-mcp` and `catalog` (now `serve mcp` and `ci --snapshot`) still run.

## Catalog plans

A catalog plan merges several operations of an API into one tool with a selector argument and excludes operations an agent cannot use. `compare --design FILE` writes a proposed plan when the file is not there (`proposed: true`), which no person has reviewed, and tests the plan against one tool per operation on the same requests. Never adopt a plan yourself: show the user `coverage`, the `verdict`, each cell's `rates` for `mirror` and `merged`, and `merged_tools`, and let them edit the file and set `server.catalog`. `wrong_operation_rate` is the share of trials with the right tool and the wrong selector value, which counts as an error. A plan that loses an operation, puts one in two tools or mixes reads with writes is refused with exit code 3.

## Check verdicts

| Verdict | Meaning | Exit code |
|---|---|---|
| `better` | the proposed description beats the baseline on held out requests | 0 |
| `no_difference` | no significant difference, keep whichever reads clearer | 0 |
| `worse` | the proposed description makes routing worse | 1 |
| `insufficient_data` | too few request templates differ for an exact sign flip test to reach p below 0.05 (six), no answer | 2 |

`check --pair` and `check --flagged` draft the clause themselves and give each pair a verdict in `pairs`, with `deploy` only when the clause beats both the baseline and the original. The top level `verdict` sums them up in the same four words: `worse` when a clause hurts, `better` when one is worth deploying, `insufficient_data` when no pair could be decided.

The baseline is a neutral rewrite of the current description of the same length, or lint's drafted description when the current one is missing or only a path. Trials from one request template fail together, so the test counts templates: an exact sign flip test on per template error differences, Holm corrected across pairs or groups. `diff` is the error rate change with a 95% interval in `ci`.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | passed |
| 1 | limits failed, or a checked description got worse |
| 2 | not enough data to decide |
| 3 | budget, cost cap, provider or config problem |

## Output

Every `--json` output is one object with `schema_version` and `command`. Errors have `error` and `exit_code`. `misroute guide --schema NAME` prints the JSON Schema. `--progress ndjson` streams progress events on stderr while stdout stays one JSON object. Results are also stored in `.misroute/`.

## MCP server

`misroute serve mcp -c misroute.yaml` exposes:

| Tool | Cost |
|---|---|
| `lint_descriptions`, `get_report`, `explain_pair`, `job_status`, `cancel_job` | free |
| `draft_description`, `estimate_check` | one call or free |
| `check_description`, `suggest_fix` | paid, `max_cost` is required |
| `start_job` | paid, `max_cost` is required, `estimate_only` returns the cost |

Use `start_job` for discover, probe, fix and improve, since they outlast a tool call, then poll `job_status`. For improve, `args` takes `models`, `helper`, `languages`, `requests_from`, `domain`, `real_requests`, `max_groups`, `styles`, `max_chars`, `out`, `fastmcp` and `drafts`, with the same meaning as the CLI options.
