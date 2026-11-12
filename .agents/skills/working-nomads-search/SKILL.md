---
name: working-nomads-search
version: 1.0.0
description: >
  Use this skill to search remote job listings worldwide via Working Nomads'
  public jobs feed, or to look up a specific posting. Remote-first roles across
  development, marketing, support, and more. Trigger phrases: remote jobs,
  work from home jobs, remote developer jobs, "any remote <role> jobs",
  look up this Working Nomads posting.
context: fork
enabled: true  # set to false to keep this portal installed but have /scrape skip it
allowed-tools: Bash(bun run .agents/skills/working-nomads-search/cli/src/cli.ts *)
---

# Working Nomads Search Skill

Search remote job listings from **[Working Nomads](https://www.workingnomads.com)** —
a remote-work job board — via its public JSON feed (`/api/exposed_jobs/`). No
authentication, no API key, and **zero runtime dependencies** — it runs with just `bun`.

> Like `freehire-search`, this skill queries a public JSON feed, so results are
> structured rather than parsed from markup. The whole feed arrives in one call,
> so this skill filters client-side (keyword, category, recency).

## When to use this skill

- Search remote openings by keyword across title, company, category, tags, and description
- Filter by Working Nomads category (e.g. Development, Marketing) or recency
- Look one Working Nomads posting up by its numeric id

## Commands

### Search job listings

```bash
bun run .agents/skills/working-nomads-search/cli/src/cli.ts search [-q "<keywords>"] [filters]
```

Key flags:
- `--query <text>` / `-q <text>` — keyword search (title, company, category, tags, location, description). Optional; omit to list the whole feed.
- `--category <name>` — category filter, e.g. `Development`, `Marketing` (comma-separated for OR; matches case-insensitively).
- `--jobage <days>` — posted within N days.
- `--page <n>` — 1-indexed page over the filtered set. Default 1.
- `--limit <n>` / `-n <n>` — results per page. Default 25.
- `--format json|table|plain` — default `json`.
- `--description-format markdown|text` — default `markdown`. `json` output only.
- `--no-description` — drop bodies for a cheap discovery pass (re-run without it, or use `detail`, for a shortlist's bodies).

**Search results already carry the full description.** Do not loop `detail`
over search hits — reach for `detail` only to look one posting up by id (e.g.
from the tracker).

### Fetch full job detail

```bash
bun run .agents/skills/working-nomads-search/cli/src/cli.ts detail <id|url> [--format json|plain]
```

`id` is the numeric `id` from a `search` result (e.g. `2016531`). You may also
pass a full `https://www.workingnomads.com/job/go/<id>/` URL. Returns the full
cleaned description, category, and location.

## Usage examples

```bash
# Remote Python roles
bun run .agents/skills/working-nomads-search/cli/src/cli.ts search -q "python" --limit 10 --format table

# Development roles posted in the last 14 days
bun run .agents/skills/working-nomads-search/cli/src/cli.ts search --category Development --jobage 14 --format table

# Discovery pass without bodies
bun run .agents/skills/working-nomads-search/cli/src/cli.ts search -q "designer" --no-description --limit 20

# Full details for a specific posting
bun run .agents/skills/working-nomads-search/cli/src/cli.ts detail 2016531 --format plain
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

- Data is from Working Nomads' public feed — no credentials required: search + detail only.
- `id` is the numeric id from the posting's `/job/go/<id>/` URL — pass it as-is to `detail`.
- `date` is the posting date (`pub_date`); it may be `null` for undated postings.
- `tags` arrive as one comma-separated string upstream; the CLI splits them into a list.
- The feed's search endpoint requires authentication, so all filtering is
  client-side in the CLI: `--query` matches case-insensitively across title,
  company, category, tags, location, and description.
- The API retries 429/5xx with exponential backoff; an unreachable API exits
  non-zero with a clear message.
