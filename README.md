# misroute

Find tool-routing failures before your agents reach production.

misroute tests how language models pick tools from an MCP server. It runs requests for every tool against the full catalog, finds the tools your models mix up, reproduces each failure in JSON mode and code mode, and tests description fixes against a controlled baseline.

## What it does

- **Discover:** runs requests for every tool and ranks the pairs your models confuse, with the direction of each leak
- **Reproduce:** checks JSON tool calls, and runs code-mode programs against recorded fake tools so nothing touches your server
- **Evaluate:** compares a boundary clause with the original description and a neutral edit of the same length, with exact statistics
- **Improve:** writes several descriptions per confused tool, races them on synthetic requests and exports the winners for review, from an MCP server or a Swagger file
- **Inspect:** shows the misrouted requests, what the model called, and where errors moved after a fix
- **Integrate:** runs from the command line, in CI, in pytest, from any agent through MCP, and from a local web UI, with a cost cap on every paid run
- **Guard:** logs real tool calls and holds a second write to a sibling tool until a person approves it

## Install

```console
$ uv tool install misroute
```

or `pip install misroute`. Behind a corporate package index, add `--index-url https://pypi.org/simple/`.

## Quick start

```console
$ export ANTHROPIC_API_KEY=...
$ misroute quick https://your-server/mcp
```

`quick` picks a model from your environment, writes `misroute.yaml`, runs lint and a discover capped at $1, and opens the report. It takes an MCP URL, a local command, an OpenAPI file, a tool list in MCP, OpenAI or Anthropic format, or nothing at all, in which case it uses the first server in your Claude, Cursor or VS Code config.

Something not connecting? Run `misroute doctor`. It checks the server, each key, the price and one call per model, and prints the fix for every problem.

The full loop on the bundled test server, with Claude Opus 5.5:

```console
$ misroute quick --demo --config-only
$ misroute lint --draft
9 tools, 2 with weak descriptions
  get_attachment  missing
    draft: Returns the attachment identified by attachment_id
  get_document  path only
    draft: Returns the full details of a single document identified by its ID

$ misroute discover
tool               sibling            score  signals                               seen in
get_attachment     get_document       0.42   wrong 0.33  hedge 0.42  removal 1.00  opus code, opus json
get_case_events    update_case_meta   0.25   wrong 0.25                            opus code
get_case_comments  get_case_events    0.25   hedge 0.25  removal 1.00              opus code, opus json

$ misroute report --pair get_attachment,get_document
$ misroute check --pair get_case_comments,get_case_events --modes json
$ misroute report
```

## Connect your tools

| Source | In misroute.yaml |
|---|---|
| MCP server over HTTP | `server: {url: https://your-server/mcp, auth: bearer_env:MCP_TOKEN}` |
| Local MCP server | `server: {command: [npx, your-mcp-server]}` |
| OpenAPI file | `server: {openapi: api.json}` |
| Tool list in MCP, OpenAI or Anthropic format | `server: {tools: tools.json}` |
| Your Claude, Cursor or VS Code config | `misroute quick --from claude` |
| Tools in Python | `Project.from_tools([...], "anthropic:claude-sonnet-5-5")` |

Add `include_tools: ["*issue*"]` to test a slice of a large server.

## Models

Write models as `provider:model`:

| Shorthand | Uses |
|---|---|
| `anthropic:claude-opus-5-5` | `ANTHROPIC_API_KEY` |
| `foundry:claude-opus-5-5` | `ANTHROPIC_FOUNDRY_API_KEY` and `ANTHROPIC_FOUNDRY_BASE_URL` |
| `bedrock:anthropic.claude-opus-5-5` | your AWS credentials |
| `azure:your-deployment` | `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT` |
| `openai:gpt-5.6` | `OPENAI_API_KEY` |
| `openrouter:anthropic/claude-opus-5.5` | `OPENROUTER_API_KEY` |
| `ollama:qwen3.5:9b`, `vllm:Qwen/Qwen3-14B`, `lmstudio:model` | a local server |

Prices come from the public OpenRouter catalog, cached for a week, and local models cost nothing. Set `price_in` and `price_out` to override. A model with no known price refuses paid runs, since the budget could not be enforced, so set a price for a private deployment, or 0 for a free one. Behind a company TLS proxy, set `system_certs: true`.

`--pilot` on `discover` and `compare` runs three real trials per model and mode first, so the estimate uses the reply sizes and the number of turns the model takes on your catalog instead of a guess. With `max_turns` above 1 and nothing measured yet, the estimate assumes every trial uses all its turns and says so. Every run stores how many turns trials took, so later estimates use that. Every paid run shows an estimate and stops at `budget_usd`, or at `--max-cost` when that is lower. The estimate is a guide, based on the reply sizes seen so far, not an upper bound, so an estimate above `--max-cost` is a warning and the run still starts. Each result carries the estimate next to the measured calls, tokens and dollars per model under `usage`. The limit is what holds: money is reserved before each call and output is capped near the limit, so a run can stop a little early but does not pass it. One gap remains: far from the limit no token cap is sent, so a single reply over 32768 tokens could overrun. On a stop, calls in flight finish, finished trials are saved as a `partial` run, and a rerun with more budget reuses them at no cost.

Models that only take function tools on the Responses API, such as `gpt-6-astra`, need `options: {api: responses}`. `misroute doctor` makes one tool call per model and names this fix when it sees the error. Write the model as a block, since a shorthand cannot carry options:

```yaml
models:
  - name: astra
    provider: azure-openai
    model: gpt-6-astra
    base_url: https://your-resource.openai.azure.com
    api_key_env: AZURE_OPENAI_API_KEY
    price_in: 5
    price_out: 15
    options: {api: responses, reasoning_effort: low}
```

On Azure this uses the `/openai/v1/` path of the resource; give a full URL containing `/openai/` in `base_url` to use another. Reasoning effort is part of what you are testing, so set it explicitly, and use the same effort you run in production.

On a corporate machine three more things commonly bite. A custom pip index hides misroute: install with `pip install misroute --index-url https://pypi.org/simple/`. A TLS-inspecting proxy breaks every HTTPS call until `system_certs: true` (or `MISROUTE_SYSTEM_CERTS=1`) is set. And an internal OpenAI-compatible relay is a plain `openai-compatible` model with `base_url` and `api_key_env` pointing at it - do not use the `openai:` shorthand for a non-OpenAI endpoint, the key will be sent to api.openai.com and refused there.

```yaml
server:
  url: https://your-server/mcp
models:
  - foundry:claude-opus-5-5
generator: openrouter:deepseek/deepseek-v4.1-flash
validator: openrouter:google/gemini-3.8-flash
modes: [json, code]
budget_usd: 5
```

The generator writes test requests and the validator keeps only those it routes to the right tool. Use a model from another family than the one under test for both, misroute warns when a role shares a family with a tested model.

Sampling defaults are `requests_per_tool: 6`, `rotations: 3` and `repeats: 2`, about 36 trials per tool per model and mode. Every score carries a Wilson 95% interval, so a rate from a thin sample shows how little it is pinned down. `misroute quick` caps rotations at 2 and repeats at 1 to stay cheap, treat its scores as leads for a full `discover` rather than measurements.

When the descriptions are the thing under test, or barely exist as in most Swagger files, write requests from the interface instead. That needs an interface that says what a tool returns: for tools with no output schema the helper sees only a name and parameters, and discover lists them under `thin_interface`. For those, add output schemas or stay with `request_source: description`. With `request_source: spec` the generator and validator see the route, parameters and returned fields, never the descriptions. `domain` adds a short glossary, as text or a file, and `request_languages` writes requests in each listed language with results per language.

```yaml
request_source: spec
request_languages: [en, no]
domain: Municipal operations. A work order is a planned job, an incident is a reported fault.
```

The validator is asked for every tool a request needs, and a request is kept only when that is its own tool and no other. A request that wants two things names two tools and is dropped with both listed. Lookup tools do not count as a second tool. The generator and the validator see each parameter with its type, the values it allows and its smallest and largest value, so requests and their slot values stay inside what the tool accepts, and an invented value such as an app name outside the allowed list is rejected. The validator also rejects a request no tool can do, one that asks for a filter or field the tool cannot express, and one that leans on an earlier reply, such as a next page with no cursor. A tool that is short of requests gets up to three rounds, and each further round shows the generator what was rejected and why. Every rejected request is kept with the tool the validator picked and its reason under `rejected_requests`. Written requests are stored and reused, so reruns compare like with like, but only under the instructions, `domain`, source and languages that wrote them. Change any of those, or upgrade to a version with new instructions, and they are written again. `request_use` in the result says how many tools got new requests and how many reused stored ones. A request carries a `{slot}` placeholder only when a parameter takes a value such as an id, so a tool with no such parameter gets complete requests instead of an invented filter. `domain` reaches both the generator and the validator, so a generic tool that takes an operation name should have its allowed operations listed there. Otherwise requests can ask for actions it does not support.

On a write request, only the writes decide the choice. A read before the write, in one turn, across turns or in one program, is not confusion. Reads spread across turns are only seen with `max_turns` above 1, otherwise the turn ends as `read_first` and counts as an error, not as a wrong pick. Tools count as writes from `readOnlyHint` and `destructiveHint`, or else from a verb in the name. `lint` and `discover` list tools under `effect_unknown` when neither says, and those count as reads, so annotate them. The score is about the tool name. Arguments are checked against each tool's input schema and reported apart, as `bad_args_rate` and the most common problems per model and mode, so a right tool called with an operation outside its enum or a limit above its maximum shows up without changing the routing score.

Some servers are built so the agent reads a catalog or a schema first and then calls the tool that does the work. List those entry points and their calls stop counting as choices:

```yaml
max_turns: 3
lookup_tools: [admin_api_catalog, "*_catalog"]
fixtures: fixtures.yaml
```

A trial that ends on a read before a write, on lookups only or with no call is an error, but it says nothing about which tool the model would pick. Each cell therefore also gives `decided`, the trials where a choice was seen, and `accuracy_decided`, the share of those that were right. A low `accuracy` next to a high `accuracy_decided` means trials ran out of turns, so raise `max_turns`.

With `lookup_tools` the first call to any other tool decides, and a trial that only ever called lookups ends as `lookup_only`, not as a wrong pick. `discover` suggests candidates under `lookup_candidates`: tools called on the way to several different targets. Each cell reports three things apart: `accuracy`, the first choice, `reached_rate`, whether the target was called at all within `max_turns`, and `bad_args_rate`. A trial stops as soon as the target or a write is called, so later turns are only paid for when the first one missed.

Later turns see fake results, never your server. By default those come from the output schema, which is thin for a catalog. `fixtures` points at a JSON or YAML file of recorded, sanitized results per tool, so the next step has something real to work from:

```yaml
admin_api_catalog:
  routes: [{method: POST, path: /api/repositories}]
azure_read:
  - args: {operation: app_logs}
    result: {lines: ["2026-11-10 started"]}
  - result: {items: []}
```

A value is the result itself, or a list of cases where the first one whose `args` all match the call answers it. Fake pagination follows the parameter: a numeric `page` or `offset` is the caller's to choose, a `limit` is honoured, and only an opaque cursor has to come from an earlier reply.

## Commands

There are twelve. Most work needs three of them: `discover` finds the confused tools, `check` tests a change, and `ci` guards a pull request.

| Command | Does |
|---|---|
| `misroute quick` | writes misroute.yaml when there is none, then lint and a capped discover, and opens the report. `--demo` uses the bundled test server, `--from claude` takes a server from your MCP client config, `--config-only` stops after writing the config |
| `misroute doctor` | checks the setup and prints a fix for each problem |
| `misroute lint` | flags missing, name only and path only descriptions and drafts replacements, free |
| `misroute discover` | ranks confused tool pairs from model behaviour. `--probe` then presses the flagged pairs with requests that borrow words from the sibling, `--pair a,b` presses the pairs you name |
| `misroute check` | tests a description change on held out requests: `--tool` and `--description` for your own text, `--pair a,b` to draft and test a boundary clause for two confused tools, `--flagged` for every flagged pair |
| `misroute improve` | writes, races and confirms new descriptions for confused tools, and exports them for review |
| `misroute compare` | tests whether routing changes when you move from one model to another |
| `misroute ci` | fails on exceeded limits, or with `--changed` on descriptions that got worse. `--snapshot` prints the tool hashes a later `--baseline` compares against |
| `misroute report` | reads results, free: a self-contained HTML report, `--pair a,b` for the misrouted requests between two tools and what the model called, `--site name=workdir` for a static site of several runs |
| `misroute requests` | `collect`, `review` and `status`: real user requests from logs, confirmed by a person |
| `misroute serve` | `serve mcp` runs misroute as an MCP server for agents, `serve ui` the local web interface, `serve guard` an MCP proxy that logs and guards real tool calls |
| `misroute guide` | prints the agent guide and JSON reference, `--schema NAME` the JSON Schema of an output |

Older names say where they went: `misroute fix` answers that it is now `misroute check --pair`. `serve-mcp` and `catalog` still run, since MCP client configs and CI workflows call them.

## Test what production sees

Routing depends on everything the model is given, not only on one server's descriptions. Three settings bring a run closer to the agent you ship:

```yaml
servers:
  - {name: github, url: https://api.githubcopilot.com/mcp, auth: bearer_env:GITHUB_TOKEN}
  - {name: tracker, command: [npx, your-tracker-mcp]}
system_prompt: agent_prompt.md
real_requests: real.jsonl
```

`servers` loads several servers into one catalog, the way an agent with all of them connected sees it. Pairs that join tools from different servers are marked `cross server`: neither server's owner would find them alone. A name two servers share gets the later server's name in front. `improve`, `guard` and `--fastmcp` work on the first server.

`system_prompt` is your agent's own system prompt, as text or a file. Without it trials run under a one line instruction, and a real prompt can move routing either way.

`real_requests` is the strongest evidence a run can have, and you do not have to write the file by hand:

```console
$ misroute requests collect --from traces.jsonl
added 212 candidates to real.jsonl, 212 waiting for review
$ misroute requests review
[1/212] set the status of case 4 to closed
  called: get_document, update_case
  right tool [update_case]:
$ misroute requests status
```

`collect` reads the guard log, or any JSON lines file where a line has the request as `text` and what was called as `calls` or `tool`. It proposes the first write, else the first call, as the right tool. That is a suggestion: what an agent called is not proof of what was right, so `review` asks a person to confirm each one, and unconfirmed lines are left out of every run. The guard records user turns when your agent passes them with `guard.user_turn(text)`. A proxy in front of an MCP server sees calls but not what the user asked, so use a trace export there. The file holds what your users wrote, keep it out of version control.

## Changing models

```console
$ misroute compare --from foundry:claude-opus-5-5 --to foundry:claude-fable-5-2 --max-cost 10
json: worse, p 0.012 over 9 differing templates. First choice right 91% with claude-opus-5-5, 84% with
claude-fable-5-2, change in error rate +7.0% (95% +2.1% to +12.4%) over 90 request wordings
  search_cases: 2 to 11 errors of 36
  newly confused: list_cases and search_cases
```

`compare` runs both models on the same requests, tools and orders and tests the difference with the request wording as the unit. `better` means the model you move to makes fewer errors, `worse` exits 1, and too few differing wordings gives no verdict and exits 2. It lists the tools whose errors moved and the pairs that became or stopped being confused. Calls already made by `discover` come from the cache.

## How discover ranks pairs

Each tool gets requests written from the user's goal, without the tool's own words. Requests run against the full tool list in rotated orders. Each pair shows three signals: the share of one tool's requests that went to the other, the share where the model called both, and the share that moved to it when the other tool was removed. The score is the larger of the first two, a measured rate with no weights, and write tools come first on ties. A pair is flagged when one model and mode got at least 6 of its requests wrong or hedged, the flag rule from the thesis, over at least two request wordings. Six errors from one wording repeated across slots and orders are one observation, so that pair is marked `one wording` and not flagged. Flagged pairs are what `discover --probe`, `check --flagged` and `improve` work on unless you name pairs. The 95% interval on a score counts request wordings, not trials, for the same reason. A request where the model called the right tool and another one is a hedge, not a wrong pick, whichever came first.

The ranking marks what it cannot vouch for: pairs resting on one wording, tools with few validated requests, and tools the validator left without any, marked UNTESTED. Synthetic rates are a routing diagnostic for finding transcripts worth reading, not a production success rate. `real_requests` is what turns them into evidence. Tools that models answer without calling anything are listed separately.

Code mode runs each program in Monty against recorded fakes and reports faked tools, pasted stubs, programs that never call anything, and invented or reused cursors. Programs that leave later pages unread are counted apart as `unread_pages_rate`, not as failures, since that is a fault only when the request needed everything.

## Check a change

`check --pair` writes one boundary sentence per tool and runs the original, a baseline and the clause on identical requests. The baseline is a neutral rewrite of the same length, or lint's drafted description when a tool has none. Trials from one request template fail together, so verdicts count templates, not trials: an exact sign flip test on per template error differences, with Holm correction across pairs. Below the number of differing templates an exact sign flip test needs to reach p below 0.05, which is six, there is no verdict. Every result also shows the error rate change with a 95% interval, a trial level mid p McNemar, errors in both directions and leakage to other tools.

The verdict answers the deploy question, not only the methodology question. Beating the baseline proves the clause's content matters beyond a same-length edit; `deploy` additionally requires the clause to beat the original description (`vs_original` in the JSON). A clause that beats the baseline while losing to the original reads `do not deploy`: the current text is load bearing and the clause adds nothing on top. That happens on long descriptions, where one appended sentence drowns - check warns when a description is more than five times the clause and suggests `improve` for a full rewrite.

`check --tool NAME --description TEXT` tests your own proposed description. It splits requests into a tuning set and a held out set, gives its verdict on the held out set only, and multiplies each verdict's p value by the number of verdicts that held out set has already given, so tuning a description against the same requests costs power. `--refresh` draws a fresh set.

## Improve descriptions

```console
$ misroute improve api.json --model foundry:claude-opus-5-5 --helper openai:gpt-5.6 --languages no,en --fastmcp --max-cost 5
```

`improve` takes the same targets as `quick`: an OpenAPI or Swagger file, an MCP server, or a tool list. `--model` is the model under test and `--helper` writes and judges the requests and writes the candidates. Use a helper from another family, so no model grades its own test. Real user requests, passed with `--real-requests`, are the strongest evidence and are reported separately. It runs in stages:

1. Audit, free: weak descriptions, names FastMCP cuts at 56 characters, POSTs that only read, overlapping routes, operations like health or auth that agents rarely need, uploads an agent cannot send, operations that return only a file, required headers that would become tool arguments, and spec errors that stop converters.
2. Discover with requests written from the interface, so descriptions under test never shape the requests.
3. Group confused tools and write up to four candidates per tool in different styles: a boundary clause, returns first, example led and minimal. The validator drops candidates that claim fields or behaviour the interface lacks.
4. Race the candidates on the tuning half of the requests, one tool at a time with the others fixed at their best.
5. Confirm on the held out half: the original, a baseline and the winner, with the template level test, Holm across groups and results per language.

The winner is exported when it beats the baseline. Otherwise the baseline is exported when it beats the original, and otherwise the original stays. Every entry carries its reason and numbers. Weak tools outside any group get a drafted description marked untested.

Results go to `improve/`:

| File | For |
|---|---|
| `descriptions.yaml` | tool name to description, for MCP servers you edit in code |
| `overlay.yaml` | per operationId: description, read and write hints, suggested exclusions and the evidence |
| `overlay.openapi.yaml` | the same descriptions as an OpenAPI Overlay 1.0 file for docs and other converters |
| `openapi.json` | only when the source needed it: the spec converted from Swagger 2.0 or repaired, which `server.py` then serves |
| `server.py` | with `--fastmcp`, a FastMCP 4.1.4 server that serves the spec with the overlay applied |

A Swagger 2.0 file is converted to OpenAPI 3 on load: definitions, body and form parameters, parameter types, response schemas, host and security definitions all move to where converters expect them. Two repairs are made to any spec, and listed in the audit: OAuth flows with no scopes get an empty set, and a path parameter spelled differently from its placeholder in the route is renamed to match, since its value could not be filled in otherwise. Headers that carry a declared API key, and `Authorization`, `Cookie`, `Accept` and `Content-Type`, are never turned into tool arguments. Inherited fields under `allOf` become arguments like any others. Your source file is not changed.

With `--fastmcp` misroute also starts the generated server, checks that names, descriptions, hints and argument names come through, and reruns the held out requests against it, since that is what agents will see. Run it with `uv run improve/server.py`, with `API_BASE_URL` and `API_HEADERS` set for the backend. Set `exclude: true` in `overlay.yaml` on operations to hide. Nothing is applied to your spec or server.

## For agents

Every command takes `--json` and prints one object with a `schema_version`. Exit codes are `0` passed, `1` limits failed or worse, `2` not enough data, `3` budget or config problem. Paid commands take `--max-cost` and `--estimate-only`, and never prompt without a terminal. `--progress ndjson` streams progress to stderr. `misroute guide --schema check` prints the output schema, and `misroute guide` prints the full agent guide, which also ships in the package and as the `misroute://guide` resource of `misroute serve mcp`.

Add misroute as an MCP server:

```console
$ claude mcp add misroute -- uvx misroute serve mcp
```

```json
{"mcpServers": {"misroute": {"command": "uvx", "args": ["misroute", "serve", "mcp"]}}}
```

The second form goes in `.cursor/mcp.json`. VS Code takes the same block under `"servers"` in `.vscode/mcp.json`.

The server exposes `lint_descriptions`, `draft_description`, `check_description`, `explain_pair`, `suggest_fix`, `get_report` and background jobs with `start_job`, `job_status` and `cancel_job` for runs longer than a tool call. Paid tools take a required `max_cost` and stop when spending reaches it. misroute never edits your tools.

Claude Code users can install the plugin, which adds the server and a skill:

```console
/plugin marketplace add philippyt/tool-misroute
/plugin install misroute@misroute
```

See [AGENTS.md](AGENTS.md) and [llms.txt](llms.txt) for the agent workflow.

## CI

Check the descriptions a pull request changed, and comment the result:

```yaml
- uses: actions/checkout@v5
  with: {fetch-depth: 0}
- uses: philippyt/tool-misroute@v0.2.4
  with:
    config: misroute.yaml
    max-cost: "2"
```

Or wire it yourself with `misroute ci --snapshot` on the base branch and `misroute ci --changed --baseline tools.json --markdown comment.md --junit results.xml`. `misroute lint --sarif lint.sarif` feeds GitHub code scanning, and `--strict` fails on weak descriptions.

As a pre-commit hook:

```yaml
- repo: https://github.com/philippyt/tool-misroute
  rev: v0.2.4
  hooks:
    - id: misroute-lint
```

## In tests

```python
def test_routing(misroute_routes):
    misroute_routes(my_tools, "anthropic:claude-sonnet-5-5", min_accuracy=0.95, max_cost=1)
```

The `misroute_routes` fixture comes with the package. It runs a discover, fails with the confused pairs when accuracy is below the `min_accuracy` you set, and caches responses so reruns are free. Outside pytest, use `from misroute.testing.routes import assert_routes`.

## Guard

As a proxy in front of any MCP server:

```console
$ misroute serve guard -c misroute.yaml
$ misroute serve guard --approve 8ba64a3fc925
```

Inside an MCP server built with the Python SDK:

```python
server = MCPServer("cases", middleware=[guard.middleware()])
```

Around tool functions in any agent framework, keeping their signatures:

```python
@guard.tool
def update_case(case_id: str, status: str) -> dict: ...
```

The guard logs every call, flags hedges and quick switches between siblings, and holds a second write to a sibling tool until it is approved in `misroute serve ui` or with `misroute serve guard --approve`. `misroute report` adds a Production section from the guard log.

## Results

Everything lands in `.misroute/`: a DuckDB database, JSONL transcripts of every run and a response cache.

```console
$ duckdb .misroute/misroute.duckdb "select target, chosen, count(*) from trials where wrong group by all order by 3 desc"
```

## Python API

```python
from misroute import Project

proj = Project.from_yaml("misroute.yaml")
found = await proj.discover()
result = await proj.fix(top=3, trials=96)
```
