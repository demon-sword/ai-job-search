---
name: job-check
version: 1.0.0
description: >
  Check live job postings in one command and get back a ranked shortlist with
  pay-vs-bar and India-eligibility verdicts per row. Triggers on: check jobs,
  job check, verify these jobs, are these jobs worth applying, pay check jobs.
context: fork
enabled: true  # set to false to keep this skill installed but have agents skip it
allowed-tools: Bash(bun run .agents/skills/job-check/cli/src/cli.ts *)
---

# Job Check Skill

One command that runs discovery + dedupe + full-text triage across the
installed portal-search CLIs and emits a ranked shortlist in a fixed format:
rank, title, company, location, score/band, pay verdict against the 55 LPA
bar, India-eligibility verdict, posting URL. Shortlist first, then
honestly-below-floor. Stubs are never shortlisted.

## Command

```bash
bun run .agents/skills/job-check/cli/src/cli.ts check -q "<role>" -l "<place>" --format table
bun run .agents/skills/job-check/cli/src/cli.ts check --input /tmp/jobs.json --format table
```

Key flags: `--query/-q`, `--location/-l` (defaults: "software engineer" /
"India"), `--jobage`, `--limit/-n` (default 20), `--portal a,b` (restrict
discovery), `--input` (offline triage of a JSON file, no network),
`--pay-bar-lpa` (default 55), `--min-score` (default 45, the Moderate Fit
floor), `--format json|table|plain` (default json). `--help` lists all flags.

## How it works

1. **Discovery.** Enumerates enabled portal skills (`.agents/skills/*/SKILL.md`
   with `enabled != false` and a `cli/src/cli.ts`), runs each portal's own
   `search` with the query, and pools the hits. Portal flags are probed from
   that portal's SKILL.md (`--location`, `--jobage`, `--limit` are only passed
   when documented there — the CLIs reject unknown flags). Portals run
   sequentially (~1s apart); a portal failure is recorded in `portal_errors`
   and never fails the run.
2. **Dedupe.** Exact-URL first, then the canonical key from
   `tools/job_key.py` (called as-is, never forked; a local slug is only the
   fallback when python is unavailable).
3. **Full-text triage.** Fetches each candidate with its portal's `detail`
   command (responses cached 7 days under
   `.agents/skills/job-check/.cache/`, ~1s between fetches). A candidate with
   no usable posting text is a **stub**: it is listed under `Stubs` and is
   never shortlisted, whatever the title match.
4. **Verdicts + ranking.** Per full-text row:
   - **PAY** — figures extracted from the JD (LPA ranges/singles, annual or
     monthly INR, annual USD at the pinned `USD_TO_INR = 83` rate in
     `src/helpers.ts`) compared against `--pay-bar-lpa` (default 55):
     `stated-pass` (band top ≥ bar), `stated-below` (whole band < bar —
     listed under Below floor, never hidden), or
     `undisclosed-needs-verification` (no figure found).
   - **GEO** — India-eligibility from location + JD text: NCR /
     India-remote / worldwide-remote PASS; outside-India or work-auth
     restriction FAIL with reason (excluded, listed with the reason);
     anything unclear FLAGs for your judgment.
   - **Score** — heuristic triage (query-term coverage + skill-list bonus,
     bands reuse `/rank`'s: Strong 75+, Good 60–74, Moderate 45–59, Weak
     30–44, Poor <30). Triage only: `/apply`'s full evaluation stays
     authoritative and always re-runs before anything is drafted.
   - **Provenance** — every row carries `full_text`/`stub` plus its text
     source (`<portal>:detail` vs `<portal>:search-snippet`).

## Report shapes

`url-reference.md` (this directory) documents the JSON report schema and the
orchestrated interfaces. `cli/README.md` documents the CLI.

## Notes

- The brief named `tools/sweep_runner.py`, `tools/sweep_auto.py`, and
  `tools/sweep_triage.py`; none exists in this repo, so discovery reuses the
  portal CLIs, dedupe reuses `tools/job_key.py`, and state/sweep reuse
  `tools/rank_state.py` conventions (`candidates`/`sweep`/`apply`) instead.
- Scores are heuristics from posting text. Stated pay is taken at face value
  from the JD — verify before negotiating. Remote postings change terms
  often; re-check eligibility at apply time.
