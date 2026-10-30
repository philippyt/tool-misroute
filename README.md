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
$ misroute init --demo
$ misroute lint --draft
9 tools, 2 with weak descriptions
  get_attachment  missing
    draft: Returns the attachment identified by attachment_id
  get_document  path only
    draft: Returns the full details of a single document identified by its ID

$ misroute discover
up to 495 model calls, up to $3.43

tool               sibling            score  signals                               seen in
get_attachment     get_document       1.00   wrong 0.33  hedge 0.42  removal 1.00  opus code, opus json
get_case_comments  get_case_events    0.50   hedge 0.25  removal 1.00              opus code, opus json
get_case_events    update_case_meta   0.25   wrong 0.25                            opus code

$ misroute explain --pair get_attachment,get_document
$ misroute fix --pair get_case_comments,get_case_events --modes json
$ misroute report
```

## Connect your tools

| Source | In misroute.yaml |
|---|---|
| MCP server over HTTP | `server: {url: https://your-server/mcp, auth: bearer_env:MCP_TOKEN}` |
| Local MCP server | `server: {command: [npx, your-mcp-server]}` |
| OpenAPI file | `server: {openapi: api.json}` |
| Tool list in MCP, OpenAI or Anthropic format | `server: {tools: tools.json}` |
| Your Claude, Cursor or VS Code config | `misroute init --from claude` |
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

Prices come from the public OpenRouter catalog, cached for a week, and local models cost nothing. Set `price_in` and `price_out` to override. Every paid run shows an estimate first and stops at `budget_usd`. Behind a company TLS proxy, set `system_certs: true`.

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

When the descriptions are the thing under test, or barely exist as in most Swagger files, write requests from the interface instead. With `request_source: spec` the generator and validator see the route, parameters and returned fields, never the descriptions. `domain` adds a short glossary, as text or a file, and `request_languages` writes requests in each listed language with results per language.

```yaml
request_source: spec
request_languages: [en, no]
domain: Municipal operations. A work order is a planned job, an incident is a reported fault.
```

## Commands

| Command | Does |
|---|---|
| `misroute quick` | lint and a capped discover in one go, then opens the report |
| `misroute init` | writes misroute.yaml from your MCP client config and environment |
| `misroute doctor` | checks the setup and prints a fix for each problem |
| `misroute lint` | flags missing, name only and path only descriptions and drafts replacements |
| `misroute discover` | ranks confused tool pairs from model behaviour |
| `misroute probe` | tests risky pairs with requests that borrow words from the sibling |
| `misroute explain` | shows the misrouted requests for a pair and what the model called |
| `misroute fix` | tests a boundary clause against a neutral edit, or against a drafted description for undescribed tools |
| `misroute check` | tests one proposed description on held out requests |
| `misroute improve` | writes, races and confirms new descriptions for confused tools, and exports them for review |
| `misroute report` | writes a self-contained HTML report |
| `misroute ui` | local web interface to set up, run, read results and approve held writes |
| `misroute ci` | fails on exceeded limits, or with `--changed` on descriptions that got worse |
| `misroute catalog` | prints tool hashes and descriptions for change-aware CI |
| `misroute serve-mcp` | runs misroute as an MCP server for agents |
| `misroute guard serve` | runs an MCP proxy that logs and guards real tool calls |
| `misroute site` | builds a static results site |
| `misroute schema` | prints the JSON Schema of a command's output |
| `misroute guide` | prints the agent guide and JSON reference |

## How discover ranks pairs

Each tool gets requests written from the user's goal, without the tool's own words. Requests run against the full tool list in rotated orders. Each pair gets a score from three signals: the share of one tool's requests that went to the other, the share where the model called both, and where requests go when one tool is removed. The score orders pairs for attention, and the rates behind it are the numbers to quote.

The ranking marks what it cannot vouch for: pairs whose request sets differ in surface words, tools with few validated requests, and tools the validator left without any, marked UNTESTED. Tools that models answer without calling anything are listed separately.

Code mode runs each program in Monty against recorded fakes and reports faked tools, pasted stubs, programs that never call anything, invented or reused cursors and unread pages.

## Fix and check

`fix` writes one boundary sentence per tool and runs the original, a baseline and the clause on identical requests. The baseline is a neutral rewrite of the same length, or lint's drafted description when a tool has none. Trials from one request template fail together, so verdicts count templates, not trials: an exact sign flip test on per template error differences, with Holm correction across pairs. Fewer than six templates that differ gives no verdict. Every result also shows the error rate change with a 95% interval, a trial level mid p McNemar, errors in both directions and leakage to other tools.

`check` tests one proposed description. It splits requests into a tuning set and a held out set, gives its verdict on the held out set only, and requires a fresh held out set after three verdicts on the same tool, so a description cannot be tuned to the test requests.

## Improve descriptions

```console
$ misroute improve api.json --model foundry:claude-opus-5-5 --helper openai:gpt-5.6 --languages no,en --fastmcp --max-cost 5
```

`improve` takes the same targets as `quick`: an OpenAPI or Swagger file, an MCP server, or a tool list. `--model` is the model under test and `--helper` writes and judges the requests and writes the candidates. Use a helper from another family, so no model grades its own test. Real user requests, passed with `--real-requests`, are the strongest evidence and are reported separately. It runs in stages:

1. Audit, free: weak descriptions, names FastMCP cuts at 56 characters, POSTs that only read, overlapping routes, operations like health or auth that agents rarely need, and spec errors that stop converters.
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
| `server.py` | with `--fastmcp`, a FastMCP 4.1.4 server that serves the untouched spec with the overlay applied |

With `--fastmcp` misroute also starts the generated server, checks that names, descriptions and hints come through, and reruns the held out requests against it, since that is what agents will see. Run it with `uv run improve/server.py`, with `API_BASE_URL` and `API_HEADERS` set for the backend. Set `exclude: true` in `overlay.yaml` on operations to hide. Nothing is applied to your spec or server.

## For agents

Every command takes `--json` and prints one object with a `schema_version`. Exit codes are `0` passed, `1` limits failed or worse, `2` not enough data, `3` budget or config problem. Paid commands take `--max-cost` and `--estimate-only`, and never prompt without a terminal. `--progress ndjson` streams progress to stderr. `misroute schema check` prints the output schema, and `misroute guide` prints the full agent guide, which also ships in the package and as the `misroute://guide` resource of `misroute serve-mcp`.

Add misroute as an MCP server:

```console
$ claude mcp add misroute -- uvx misroute serve-mcp
```

```json
{"mcpServers": {"misroute": {"command": "uvx", "args": ["misroute", "serve-mcp"]}}}
```

The second form goes in `.cursor/mcp.json`. VS Code takes the same block under `"servers"` in `.vscode/mcp.json`.

The server exposes `lint_descriptions`, `draft_description`, `check_description`, `explain_pair`, `suggest_fix`, `get_report` and background jobs with `start_job`, `job_status` and `cancel_job` for runs longer than a tool call. Paid tools refuse to run above `max_cost`. misroute never edits your tools.

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

Or wire it yourself with `misroute catalog` on the base branch and `misroute ci --changed --baseline tools.json --markdown comment.md --junit results.xml`. `misroute lint --sarif lint.sarif` feeds GitHub code scanning, and `--strict` fails on weak descriptions.

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
    misroute_routes(my_tools, "anthropic:claude-sonnet-5-5", min_accuracy=0.95)
```

The `misroute_routes` fixture comes with the package. It runs a small capped discover, fails with the confused pairs when accuracy is below the threshold, and caches responses so reruns are free. Outside pytest, use `from misroute.testing.routes import assert_routes`.

## Guard

As a proxy in front of any MCP server:

```console
$ misroute guard serve -c misroute.yaml
$ misroute guard confirm 8ba64a3fc925
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

The guard logs every call, flags hedges and quick switches between siblings, and holds a second write to a sibling tool until it is approved in `misroute ui` or with `misroute guard confirm`. `misroute report` adds a Production section from the guard log.

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
