# Job Check Interface Reference

Interfaces this skill orchestrates. It calls them as-is and never forks them.

## Portal search CLIs (discovery + full text)

Every enabled skill under `.agents/skills/<portal>/` with `cli/src/cli.ts`:

```
bun run .agents/skills/<portal>/cli/src/cli.ts search --query "<q>" --format json
bun run .agents/skills/<portal>/cli/src/cli.ts detail <id|url> --format json
```

`search` returns `{ "results": [...] }` (or a bare array) with `title`,
`company`, `location`, `url`, `date`, and a `snippet`/`description` preview.
`detail` returns the full posting; the skill best-effort reads `description`,
`full_description`, `text`, `body`, or `snippet` (also under a nested
`result`/`job` object), else falls back to long plain-text output. Only
`--location`, `--jobage`, and `--limit` flags documented in that portal's
`SKILL.md` are passed — undocumented flags are rejected by the CLIs.

## tools/job_key.py (dedupe)

```
python3 tools/job_key.py --company "<company>" --title "<title>"
```

Prints the canonical dedupe key on stdout, exit 0. The skill takes the last
non-empty stdout line as the key.

## tools/rank_state.py (state conventions)

Score bands and veto semantics follow `/rank` (`rank_state.py`
`candidates`/`sweep`/`apply`): Strong Fit 75+, Good Fit 60–74, Moderate Fit
45–59, Weak Fit 30–44, Poor Fit <30; location FAIL vetoes the shortlist,
FLAG stays ranked with a marker. This skill does not write
`job_scraper/seen_jobs.json` — use `/rank` when scoring should persist.

## JSON report schema

```json
{
  "shortlist":   [{ "rank": 1, "title": "…", "company": "…", "location": "…",
                    "score": 72, "band": "Good Fit",
                    "pay_verdict": "stated-pass | stated-below | undisclosed-needs-verification",
                    "pay_evidence": ["60-70 LPA"],
                    "geo_verdict": "PASS | FAIL | FLAG", "geo_reason": "…",
                    "url": "https://…", "provenance": "full_text",
                    "text_source": "<portal>:detail" }],
  "below_floor": ["same rows — pay stated below the bar, or under --min-score"],
  "excluded":    [{ "title": "…", "company": "…", "geo_verdict": "FAIL",
                    "geo_reason": "…", "url": "https://…" }],
  "stubs":       [{ "title": "…", "company": "…", "url": "https://…",
                    "provenance": "stub", "text_source": "<portal>:search-snippet",
                    "reason": "…" }],
  "portal_errors": [{ "portal": "…", "error": "…" }],
  "meta": { "pay_bar_lpa": 55, "min_score": 45, "query": "…",
            "candidates": 0, "full_text": 0, "stubs": 0 }
}
```

Table format (`--format table`) prints the same sections as
`rank | score/band | title | company | location | pay | india-eligible | url`
rows: Shortlist, Below floor, Excluded, Stubs (empty sections omitted except
Shortlist). Stubs never appear in Shortlist.
