---
name: remoteok-search
version: 1.0.0
description: >
  Use this skill to search remote job listings worldwide via RemoteOK's public
  API, or to look up a specific posting. Remote-first roles across software,
  data, design, marketing, support, and more. Trigger phrases: remote jobs,
  work from home jobs, remote developer jobs, "any remote <role> jobs",
  look up this RemoteOK posting.
context: fork
enabled: true  # set to keep this portal installed but have /scrape skip it, set to false
allowed-tools: Bash(bun run .agents/skills/remoteok-search/cli/src/cli.ts *)
---

enabled: true  # set to false to keep this portal installed but have /scrape skip it

Search remote job listings from **[RemoteOK](https://remoteok.com)** — a
remote-work job board — via its public JSON API. No authentication, no API key,
and **zero runtime dependencies** — it runs with just `bun`.

> Like `freehire-search`, this skill queries a public JSON API, so results are
> structured rather than parsed from markup. The whole feed arrives in one call,
> so this skill filters client-side (keyword, recency).

## Attribution (API terms)

RemoteOK's API terms require every user of the data to **link back (with
follow, without nofollow) to the URL on RemoteOK and mention RemoteOK as a
source** — both honored here: every result's `url` is the canonical RemoteOK
posting URL, and its jobs must be credited to RemoteOK when presented. Do not
use the RemoteOK logo (registered trademark).

## When to use this skill

- Search remote openings by keyword across position, company, tags, and description
- Filter by recency (posted within N days)
- Look one RemoteOK posting up by its id or slug

## Commands

### Search job listings

```bash
bun run .agents/skills/remoteok-search/cli/src/cli.ts search [-q "<keywords>"] [filters]
```

Key flags:
- `--query <text>` / `-q <text>` — keyword search (position, company, tags, location, description). Optional; omit to list the whole feed.
- `--jobage <days>` — posted within N days.
- `--page <n>` — 1-indexed page over the filtered set. Default 1.
- `--limit <n>` / `-n <n>` — results per page. Default 25.
- `--format json|table|plain` — default `json`.
- `--description-format markdown|text` — default `markdown`. `json` output only.
- `--no-description` — drop bodies for a cheap discovery pass (re-run without it, or use `detail`, for a shortlist's bodies).

**Search results already carry the full description.** Do not loop `detail`
over search hits — reach for `detail` only to look one posting up by id/slug
(e.g. from the tracker).

### Fetch full job detail

```bash
bun run .agents/skills/remoteok-search/cli/src/cli.ts detail <id|slug|url> [--format json|plain]
```

`id` is the `id` from a `search` result (e.g. `1137714`); a slug
(`remote-data-engineer-addepto-1137714`) or full posting URL works too. Returns
the full cleaned description, tags, salary, and apply URL.

## Usage examples

```bash
# Remote Python roles
bun run .agents/skills/remoteok-search/cli/src/cli.ts search -q "python" --limit 10 --format table

# Roles posted in the last 14 days
bun run .agents/skills/remoteok-search/cli/src/cli.ts search --jobage 14 --limit 10 --format table

# Discovery pass without bodies
bun run .agents/skills/remoteok-search/cli/src/cli.ts search -q "designer" --no-description --limit 20

# Full details for a specific posting
bun run .agents/skills/remoteok-search/cli/src/cli.ts detail 1137714 --format plain
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

- Data is from RemoteOK's public API — no credentials required: search + detail only.
- `date` is the posting date; it may be `null` for undated postings.
- `location` is often empty upstream (remote roles omit it) — `null` then, never guessed.
- The first array element of the API response is a legal notice, not a job; the
  CLI skips entries without an `id`.
- `salary_min`/`salary_max` of `0` upstream mean "not disclosed" and surface as `null`.
- The API retries 429/5xx with exponential backoff; an unreachable API exits
  non-zero with a clear message.
