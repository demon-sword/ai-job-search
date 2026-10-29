# wellfound-apply CLI

Captain-approved Wellfound submits. Zero runtime dependencies — runs on `bun`
alone. Drives the captain's logged-in browser profile through
`chrome-devtools-axi`; never handles passwords.

## Commands

```bash
bun run src/cli.ts queue list [--format json|table] [--state-dir <dir>]
bun run src/cli.ts queue approve --job <id> --cv <pdf> --cover <pdf> --by "<name>"
bun run src/cli.ts answers check --job <id>
bun run src/cli.ts status [--format json|table]
bun run src/cli.ts submit --job <id> [--live] [--max-per-day <1-10>]
```

`--state-dir` defaults to `.agents/skills/wellfound-apply/pilot` resolved
from the repo root; run from the repo root.

## Gate order in `submit`

Approval → documents exist → locked answers cover `requiredAnswers` →
live-cap (live only) → session attach → browser walk → receipt. The first
failing gate exits 1 with a JSON error on stderr and no browser step runs.

## Develop

```bash
bun test --timeout 30000
bunx tsc --noEmit
```
