# misroute
Find the tools your agent confuses, and test that a fix helps before you deploy it.

misroute connects to any MCP server, writes test requests for every tool, and runs them on your models in both JSON mode and code mode. It shows where each request ends up, which tools get called together, and where code-mode programs fail. For the worst cases it writes a fix and compares it with a neutral edit of the same length that adds no new information, so you only deploy a fix when its content makes the difference.

## Highlights

- **No pair list needed.** misroute watches how your models behave and finds the confusions from that, including between tools with unrelated names.
- **JSON mode and code mode.** Code-mode programs run in a [Monty](https://github.com/pydantic/monty) sandbox against recorded fake tools, so nothing touches your server.
- **Hedging counts as a failure.** Calling both sibling tools gets flagged, and in red when the tools write data.
- **Fixes tested against a neutral edit.** Every boundary clause is compared with an edit of the same length, with errors tracked in both directions and leakage to every other tool.
- **Any model.** Anthropic, Azure Foundry, Bedrock, OpenRouter, vLLM, Ollama, LM Studio and any OpenAI-compatible endpoint.
- **Private.** Runs on your machine or in CI, and data only goes to the endpoints you configure.
- **Cost-aware.** Shows an estimate before every paid run, stops at a hard budget, and caches responses so reruns are free.
- **Production guard.** An MCP proxy that logs every real call and holds a second write to a sibling tool until someone approves it.

## Installation

```console
$ uv tool install git+https://github.com/philippyt/tool-misroute
```

Or with pip:

```console
$ pip install git+https://github.com/philippyt/tool-misroute
```

Or with Docker:

```console
$ docker run -v "$PWD:/work" ghcr.io/philippyt/misroute discover
```

## Quick start

Example output from the bundled test server, run with Claude Opus 5.5 through OpenRouter pinned to the Anthropic provider:

```console
$ misroute init
wrote misroute.yaml

$ misroute lint --draft
9 tools, 2 with weak descriptions
  get_attachment  missing
    draft: Returns the attachment identified by attachment_id
  get_document  path only
    draft: Returns the full details of a single document identified by its ID

$ misroute discover
495 model calls, est $3.43
continue [y/N]: y

tool               sibling            score  signals                               seen in
get_attachment     get_document       1.00   wrong 0.33  hedge 0.42  removal 1.00  opus code, opus json
get_case_comments  get_case_events    0.50   hedge 0.25  removal 1.00              opus code, opus json
get_case_events    update_case_meta   0.25   wrong 0.25                            opus code

$ misroute fix --pair get_case_comments,get_case_events --modes json
tool               sibling          model  mode  original  baseline     with clause  p      verdict
get_case_comments  get_case_events  opus   json  10        13 neutral   6            0.065  no difference, consider a guard rule or merging the tools

$ misroute report
wrote report.html
```

## Commands

| Command | What it does |
|---|---|
| `misroute lint` | Flags tools with missing, name-only or path-only descriptions, and drafts replacements for you to review |
| `misroute discover` | Runs requests for every tool against the full tool list and ranks the risky pairs |
| `misroute probe` | Tests risky pairs with harder requests that borrow words from the sibling tool |
| `misroute fix` | Compares a boundary clause with a neutral edit, or with a drafted description for undescribed tools |
| `misroute report` | Writes a self-contained HTML report |
| `misroute ui` | Opens a local web interface for results, runs and held writes |
| `misroute ci` | Runs discover and exits with an error when limits are exceeded |
| `misroute guard serve` | Runs an MCP proxy that logs and guards every real tool call |
| `misroute site` | Builds a static results site from one or more runs |

## Status

| Feature | Status |
|---|---|
| lint, discover, probe and fix in JSON and code mode | tested on Opus 5.5 through OpenRouter and on a local model through Ollama |
| OpenAI compatible adapter | tested on OpenRouter and Ollama |
| Native Anthropic, Foundry and Bedrock adapters | not yet tested against real endpoints |
| Near misses | experimental, not yet validated, kept out of the ranking |
| Local UI | API and pages checked in a browser, not yet used on a real project |
| Guard proxy | tested against a local test server only |

## Features

### Discover
Each tool gets requests that describe what the user wants without using the tool's own words. A second model keeps a request only if it picks the right tool, and a permutation gate checks that the request sets can't be told apart by length or word overlap alone. Requests run against the full tool list in rotated orders, because models pick the wrong tool more often when the distractor comes first.

Discover combines four signals:

- **Confusion matrix:** where each tool's requests land, including tools with unrelated names
- **Co-calls:** tools called together in one turn or one program
- **Removal test:** remove a tool, rerun its requests and see where they go
- **Near misses:** the probability spread across tools for local models through vLLM, and disagreement across repeated runs for hosted models

You can add real questions from your logs with `real_requests: logs.jsonl`. misroute reports them separately from the generated ones.

### Code mode

In code mode the model gets a single `run_code` tool, with your tools available as async Python functions. misroute runs each program in Monty against recorded fakes and judges it by the calls it actually made. It catches:

- faked tools that return invented data
- pasted stubs that shadow the real function
- programs that never call anything
- invented or reused pagination cursors, and pages left unread
- unknown functions, syntax errors and runtime errors
- answers with no program at all

### Fix

For each risky pair, misroute writes a boundary clause: one sentence per tool saying what it covers and when the other tool is the better choice. For best results, use a model from a different family to write it. Each pair then runs three ways on the same requests: the original description, a baseline, and the baseline with the clause. The baseline is a neutral edit of the same length. When lint flags a tool as undescribed, the baseline is lint's drafted description instead, since writing any description already adds information. That tells you whether the clause adds anything beyond a basic description.

- Per-pair verdicts use an exact McNemar test with Holm correction.
- Across pairs, misroute uses an exact sign-flip test.
- Errors are reported in both directions, along with any leakage to other tools.
- When the clause does no better than the baseline, misroute tells you and suggests a guard rule or merging the tools.

`misroute fix --cheap` runs fewer trials and gives indicative numbers.

### Guard

Put misroute in front of your MCP server to watch real traffic:

```console
$ misroute guard serve -c misroute.yaml
$ misroute guard confirm 8ba64a3fc925
approved 8ba64a3fc925
```

Or wrap a client session in Python:

```python
from misroute.guard.wrapper import wrap

guard = wrap(session, rules, ".misroute/guard.jsonl")
await guard.call_tool("update_case", {"case_id": "7", "status": "closed"})
```

Guard logs every call, including calls made inside code-mode programs. It flags hedging, quick switches between sibling tools and faked tools, and it holds a second write to a sibling tool until a person approves it. To send logs to OpenTelemetry, set `guard: {otel: true}`. Spotting users who rephrase their question needs the user's messages, so that only works through the Python wrapper.

### CI

```yaml
limits:
  max_wrong_rate: 0.05
  max_write_hedge_rate: 0.0
  max_code_failure_rate: 0.10
```

```console
$ misroute ci
opus json wrong rate 0.105 above 0.05
```

A cheap local model catches regressions in its own behaviour, which may differ from your production model's. Schedule regular runs against the model you actually deploy as well.

## Configuration

```yaml
server:
  url: https://mcp.example.com/mcp      # or command: [npx, your-server]
  auth: bearer_env:MCP_TOKEN

models:
  - name: opus-eu
    provider: anthropic-foundry         # anthropic, anthropic-foundry, bedrock, openai-compatible
    model: claude-opus-5-5
    price_in: 4
    price_out: 20
  - name: local
    provider: openai-compatible
    base_url: http://localhost:8000/v1
    model: Qwen/Qwen3-14B
    scoring: true                       # near misses from token probabilities, vLLM only

generator: local                        # writes the test requests
validator: opus-eu                      # checks them
writer: local                           # writes clauses and neutral edits

modes: [json, code]
budget_usd: 5.00
```

Results are saved in `.misroute/`: a DuckDB database you can query with SQL, JSONL transcripts of every run, and a response cache.

```console
$ duckdb .misroute/misroute.duckdb "select target, chosen, count(*) from trials where wrong group by all order by 3 desc"
```

## Python API

```python
from misroute import Project

proj = Project.from_yaml("misroute.yaml")
found = await proj.discover(models=["local"])
result = await proj.fix(top=3, trials=96)
```