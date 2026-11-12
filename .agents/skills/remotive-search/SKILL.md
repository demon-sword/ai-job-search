---
name: remotive-search
version: 1.0.0
description: >
  Use this skill to search remote job listings worldwide via Remotive's public
  jobs API, or to look up a specific posting. Remote-first roles across
  software, data, marketing, support, and more. Trigger phrases: remote jobs,
  work from home jobs, distributed team roles, remote developer jobs,
  "any remote <role> jobs", look up this Remotive posting.
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/remotive-search/cli/src/cli.ts *)
---

# Remotive Search Skill

Search remote job listings from **[Remotive](https://remotive.com)** — a
remote-work job board — via its public JSON API. No authentication, no API key,
and **zero runtime dependencies** — it runs with just `bun`.

> Like `freehire-search`, this skill queries a public JSON API, so results are
> structured rather than parsed from markup. Unlike freehire, Remotive's API
> returns the whole feed in one call and ignores server-side filtering, so this
> skill filters client-side (keyword, category, recency).

## Attribution (API terms)

Remotive grants API access so developers can share its jobs further, on two
conditions, both honored here: **link back to the URL found on Remotive** (every
result's `url` is the canonical Remotive posting URL) **and mention Remotive as
a source** when presenting its jobs. Do not submit Remotive jobs to third-party
job websites (Jooble, Neuvoo, Google Jobs, LinkedIn Jobs).

## When to use this skill

- Search remote openings by keyword across title, company, tags, and description
- Filter by Remotive category (e.g. `software-dev`, `data`, `marketing`) or recency
- Look one Remotive posting up by its numeric id (including one that has
  already rotated out of the feed window, via the cached search — see Notes)

## Commands

### Search job listings

```bash
bun run .agents/skills/remotive-search/cli/src/cli.ts search [-q "<keywords>"] [filters]
```

Key flags:
- `--query <text>` / `-q <text>` — keyword search (title, company, tags, category, description). Optional; omit to list the whole feed.
- `--category <name>` — Remotive category slug, e.g. `software-dev`, `data`, `marketing`, `customer-service`. Repeatable via comma list.
- `--jobage <days>` — posted within N days.
- `--page <n>` — 1-indexed page over the filtered set. Default 1.
- `--limit <n>` / `-n <n>` — results per page. Default 25.
- `--format json|table|plain` — default `json`.
- `--description-format markdown|text` — `markdown` keeps headings/lists; `text` is plain paragraphs. Default `markdown`. `json` output only.
- `--no-description` — drop bodies for a cheap discovery pass (re-run without it, or use `detail`, for a shortlist's bodies).

**Search results already carry the full description.** Do not loop `detail`
over search hits — reach for `detail` only to look one posting up by id (e.g.
from the tracker).

### Fetch full job detail

```bash
bun run .agents/skills/remotive-search/cli/src/cli.ts detail <id|url> [--format json|plain]
```

`id` is the numeric `id` from a `search` result (e.g. `2091171`). You may also
pass a full `https://remotive.com/remote-jobs/...` URL (the trailing numeric id
is extracted). Returns the full cleaned description, tags, job type, salary, and
location requirement.

## Usage examples

```bash
# Remote backend roles
bun run .agents/skills/remotive-search/cli/src/cli.ts search -q "backend engineer" --limit 10 --format table

# Software-dev roles posted in the last 14 days
bun run .agents/skills/remotive-search/cli/src/cli.ts search --category software-dev --jobage 14 --format table

# Data roles, discovery pass without bodies
bun run .agents/skills/remotive-search/cli/src/cli.ts search -q "data analyst" --no-description --limit 20

# Full details for a specific posting
bun run .agents/skills/remotive-search/cli/src/cli.ts detail 2091171 --format plain
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

- Data is from Remotive's public API — no credentials required: search + detail only.
- `id` in search results is Remotive's numeric job id — pass it as-is to `detail`.
- `date` is the posting date (`publication_date`); it may be `null` for undated postings.
- The feed endpoint ignores query parameters, so all filtering is client-side in
  the CLI: `--query` matches case-insensitively across title, company, tags,
  category, and description.
- **Feed stub (2026-11-12): the API currently serves ~14 jobs, not the
  browsable site's full corpus** — coverage is whatever the stub window holds.
  `detail` on an id outside the window returns `NOT_FOUND` rather than guessing.
- The API retries 429/5xx with exponential backoff; an unreachable API exits
  non-zero with a clear message.
