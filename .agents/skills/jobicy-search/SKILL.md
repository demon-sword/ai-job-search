---
name: jobicy-search
version: 1.0.0
description: >
  Use this skill to search remote job listings worldwide via Jobicy's public
  API, or to look up a specific posting. Remote-first roles across software,
  data, marketing, support, and more. Trigger phrases: remote jobs,
  work from home jobs, remote developer jobs, "any remote <role> jobs",
  look up this Jobicy posting.
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/jobicy-search/cli/src/cli.ts *)
---

# Jobicy Search Skill

Search remote job listings from **[Jobicy](https://jobicy.com)** — a remote-work
job board — via its public JSON API. No authentication, no API key, and **zero
runtime dependencies** — it runs with just `bun`.

> Like `freehire-search`, this skill queries a public JSON API, so results are
> structured rather than parsed from markup. Server-side filters (`--tag`,
> `--geo`, `--industry`) narrow the 7-day feed window; `--query` refines
> client-side.

## Attribution (API terms)

Jobicy's API terms ask that Jobicy be clearly credited with a direct link to
the source and that applications go through the original job URL — both honored
here: every result's `url` is the canonical Jobicy posting URL.

## When to use this skill

- Search remote openings by keyword (`--tag` matches the job title server-side)
- Filter by geography (`--geo`), industry (`--industry`), or recency
- Look one Jobicy posting up by its id or slug, including its live open/closed status

## Commands

### Search job listings

```bash
bun run .agents/skills/jobicy-search/cli/src/cli.ts search [-q "<keywords>"] [filters]
```

Key flags:
- `--query <text>` / `-q <text>` — keyword search refined client-side across title, company, excerpt, and description.
- `--tag <tag>` — server-side keyword filter applied to the job title (e.g. `python`, `marketing`).
- `--geo <code>` — server-side country filter, e.g. `canada`, `usa`.
- `--industry <slug>` — server-side category filter, e.g. `engineering`, `supporting`.
- `--jobage <days>` — posted within N days (the feed itself spans 7 days).
- `--page <n>` — 1-indexed page, walked via cursor pagination. Default 1.
- `--limit <n>` / `-n <n>` — results per page (max 100). Default 25.
- `--format json|table|plain` — default `json`.
- `--description-format markdown|text` — default `markdown`. `json` output only.
- `--no-description` — drop bodies for a cheap discovery pass (re-run without it, or use `detail`, for a shortlist's bodies).

**Search results already carry the full description.** Do not loop `detail`
over search hits — reach for `detail` only to look one posting up by id/slug
(e.g. from the tracker) or to check whether it is still open.

### Fetch full job detail

```bash
bun run .agents/skills/jobicy-search/cli/src/cli.ts detail <id|slug|url> [--format json|plain]
```

`id` is the numeric `id` from a `search` result (e.g. `161642`); a slug
(`161642-forward-deployed-engineer-10`) or full posting URL works too. Returns
the full cleaned description plus the live `status` (`active` / `closed` /
`unknown`) from Jobicy's batch status endpoint.

## Usage examples

```bash
# Remote Python roles
bun run .agents/skills/jobicy-search/cli/src/cli.ts search --tag python --limit 10 --format table

# Engineering roles open to applicants in the USA
bun run .agents/skills/jobicy-search/cli/src/cli.ts search --geo usa --industry engineering --limit 10 --format table

# Keyword refinement on top of a tag
bun run .agents/skills/jobicy-search/cli/src/cli.ts search --tag python -q "senior" --limit 10

# Full details plus live status for a specific posting
bun run .agents/skills/jobicy-search/cli/src/cli.ts detail 161642 --format plain
```

## Output formats

| Format | Best for |
|--------|----------|
| `json` | Default — programmatic use; the only format carrying each hit's description |
| `table` | Quick human-readable scanning |
| `plain` | Reading a single job's full detail (`detail` command) |

Search JSON is `{ "meta": { "count", "page", "total" }, "results": [...] }`; each
result carries at least `id`, `title`, `company`, `location`, `date`, `url`, and
`description` (missing values are `null`). `table` and `plain` omit the
description — it would swamp a scannable list. All errors are written to
**stderr** as `{ "error": "...", "code": "..." }` and the process exits with code `1`.

## Notes

- Data is from Jobicy's public API — no credentials required: search + detail only.
- `date` is the posting date (`pubDate`); it may be `null` for undated postings.
- The feed covers the last 7 days; older postings 404 out of search, and
  `detail` then reports `NOT_FOUND`.
- `detail`'s `status` comes from `/api/jobs/status?ids=` — `active` means the
  posting is still listed, `closed` means it is gone, `unknown` means the status
  endpoint had no record.
- The API retries 429/5xx with exponential backoff; an unreachable API exits
  non-zero with a clear message.
