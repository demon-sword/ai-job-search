# job-check-cli

One command that checks live job postings and emits a ranked shortlist with
pay-vs-bar and India-eligibility verdicts per row.

**Data source**: the installed portal-search CLIs under `.agents/skills/*/`
plus `tools/job_key.py` for dedupe — orchestrated as-is, never forked.
**Authentication**: none. **Dependencies**: none (plain `bun` + `fetch`).
`bun install` is optional and only pulls dev type defs.

> Keep volume low on live discovery: portals run sequentially with ~1s
> pacing, detail responses are cached for 7 days, and every portal failure is
> recorded without failing the run.

## Installation

```bash
cd .agents/skills/job-check/cli
bun install   # optional — only installs TypeScript dev types
```

The CLI runs without any install because it has zero runtime dependencies.

## Commands

| Command | Description |
|---------|-------------|
| `check` | Run discovery + dedupe + full-text triage, print the ranked shortlist |

`check` accepts `--format json|table|plain` (default `json`).
All errors are written to **stderr** as `{ "error": "...", "code": "..." }`
with exit code `1`.

## Quick examples

```bash
# Live check: data roles in India as a table
bun run src/cli.ts check -q "data engineer" -l "India" --format table

# Offline triage of a candidate file (no network)
bun run src/cli.ts check --input /tmp/jobs.json --pay-bar-lpa 55 --format table

# Only one portal, lower shortlist floor
bun run src/cli.ts check -q "backend engineer" --portal linkedin-search --min-score 30
```

`--input` entries look like
`{"title": "…", "company": "…", "location": "…", "url": "https://…", "text": "full JD text"}` —
entries without usable `text` are reported as stubs, never shortlisted.

## Verdicts

- **pay**: `stated-pass` (stated band top ≥ `--pay-bar-lpa`, default 55),
  `stated-below` (whole band below the bar → Below floor section),
  `undisclosed-needs-verification` (no figure in the JD).
- **india-eligible**: `PASS` (NCR / India-remote / worldwide-remote),
  `FAIL` (outside India or work-auth restricted → Excluded with reason),
  `FLAG` (unclear — verify before applying).
- **score/band**: heuristic triage from posting text (Strong 75+, Good 60–74,
  Moderate 45–59, Weak 30–44, Poor <30). `/apply` re-evaluates before
  anything is drafted.
