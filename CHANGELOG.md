# Changelog

## 0.4.0

This release changes scoring, output and command names. Results from 0.3 are treated as stale and written again.

### Breaking

- `schema_version` is 2 in every `--json` output and stored result.
- Twelve commands instead of 21. Old names answer with the new form and exit 3: `init` is `quick --config-only`, `probe` is `discover --probe`, `fix` is `check --pair`, `explain` is `report --pair`, `site` is `report --site`, `schema` is `guide --schema`, `ui` and `guard` are `serve ui` and `serve guard`. `serve-mcp` and `catalog` still run, as `serve mcp` and `ci --snapshot`.
- A pair is flagged only when its 6 errors span at least two request wordings. Six errors from one wording are marked `one_wording`. Intervals count request wordings, not trials.
- A trial where the right tool was called second is a hedge, not a wrong pick.
- The surface gate is no longer run in discover. `gate_failed` is gone from its output.
- `--max-cost` limits actual spend. An estimate above it is a warning and the run still starts.
- Unread pages in code mode are counted apart as `unread_pages_rate`, not as a failure.
- The schemas `fix` and `explain` are now `check_pair` and `report_pair`.
- Written requests are stored under the instructions, domain and slots that wrote them, so requests from 0.3 are not reused.

### Added

- `compare --from OLD --to NEW`: does routing change between two models, on the same requests.
- `compare --design catalog.yaml`: catalog plans that merge several operations into one tool with a selector, tested against one tool per operation. Every operation stays reachable or is excluded with a reason. `server.catalog` adopts a plan.
- `requests collect`, `review` and `status`: real user requests from guard logs and traces, confirmed by a person.
- `lookup_tools` for entry points such as a catalog, `fixtures` for recorded tool results, `system_prompt` for the agent's own prompt, and `servers` for several servers in one catalog.
- Swagger 2.0 files are converted to OpenAPI 3 on load. Specs that converters reject are repaired: OAuth flows with no scopes, operations with no responses, responses with no description and path parameters spelled differently from the route.
- Call arguments are checked against each tool's input schema and reported as `bad_args_rate`.
- Cells report `decided`, `accuracy_decided`, `reached_rate` and `wrong_operation_rate`.
- `--pilot` measures reply sizes and turns before the estimate. Results carry `estimate` and measured `usage` per model, with reasoning tokens where the provider gives them.
- The audit notes file uploads, operations that return only a file and required headers that became tool arguments.

### Fixed

- A numeric `page` or `offset` is no longer treated as an invented cursor, and a page size is honoured.
- Requests no longer force a placeholder on tools with no value parameter, lean on an earlier reply, ask for more than one tool or name a value a parameter does not allow.
- A tool with no valid requests gets up to three rounds, with the validator's rejections fed back.
- The estimate no longer multiplies by `max_turns`. It uses the turns trials actually took.
- Header parameters that carry a declared API key are no longer tool arguments.
- Deep schemas are no longer cut off at six levels of nesting.
