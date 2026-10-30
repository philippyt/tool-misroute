---
name: misroute
description: Check MCP tool descriptions against model behaviour with the misroute CLI. Use after editing a tool name or description on an MCP server, when asked whether agents pick the wrong tool, or before merging changes to tool definitions.
---

# misroute

1. Without a misroute.yaml, run `misroute quick <server url, OpenAPI file or command> --json --max-cost 1 --no-open`. If something fails to connect, run `misroute doctor --json` and follow the fix it prints.
   Otherwise run `misroute lint --json -c misroute.yaml`. It is free. Fix missing and path only descriptions first.
   To see why two tools are confused, run `misroute explain --pair a,b --json`.
2. After you change one description, run
   `misroute check --tool NAME --description "NEW TEXT" --json --max-cost 1 -c misroute.yaml`.
3. Read `verdict`:
   - `better`: propose the change to the user with `wrong_rate`, `trials` and `held_out`.
   - `no_difference` or `worse`: do not claim an improvement. Show the numbers.
   - `insufficient_data`: say the sample was too small. Do not read anything into the rates.
4. To improve many descriptions at once, for an MCP server or a Swagger file, run
   `misroute improve <target> --json --max-cost 5`. Propose only entries with status `tested` or `tested baseline` as tested, with their `confirm.tests` numbers. Present `draft` entries as untested drafts.
5. Never apply a description to the server yourself. Propose it and let the user apply it.
6. Do not run `check` repeatedly on the same tool to tune wording. Each verdict on the same held out set multiplies its p value by the number of verdicts given (`looks`). Only pass `--refresh` when the user agrees to a fresh set.

Exit codes: 0 passed, 1 worse or limits failed, 2 insufficient data, 3 budget or config problem.
If a run exits with 3 because of cost, report the estimate and ask before raising `--max-cost`.
