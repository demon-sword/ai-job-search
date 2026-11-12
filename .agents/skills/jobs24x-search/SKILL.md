---
name: jobs24x-search
version: 1.0.0
description: >
  Use this skill to search job listings on jobs24x (tech and remote roles), or
  to look up a specific posting. Free-surface results are paywalled (locked:
  no company or apply URL) — see Auth gap. Trigger phrases: jobs24x search,
  jobs24x jobs, "any jobs on jobs24x", look up this jobs24x posting.
context: fork
enabled: false  # free surface is paywalled (locked rows only) — /scrape skips this portal until paid access is configured; see Auth gap below
allowed-tools: Bash(bun run .agents/skills/jobs24x-search/cli/src/cli.ts *)
---

# jobs24x Search Skill

Search job listings from **[jobs24x](https://jobs24x.com/jobs)** — a tech/remote
job board aggregating company career pages — through the same encrypted edge
functions the public site itself calls. **Zero runtime dependencies** — it runs
with just `bun`.

> Unlike the other portal skills, this one currently returns **locked rows**:
> titles, snippets, and dates are visible, but the company name and apply URL
> require paid access. The skill is installed but `enabled: false`, so `/scrape`
> skips it and reports it as skipped rather than silently collecting junk.

## Auth gap (read before using)

- **Free surface (no account):** the site's `list-jobs` function answers
  anonymous callers, but every row comes back `locked: true` with the apply
  URL replaced by `"#locked"` and the company name redacted (often the
  placeholder `"Jobs"` or a title fragment — never the real employer). There is
  no usable company or URL on the free surface: no account creation, no payment,
  and no page rendering changes that — the lock is server-side (`isPaid: false`
  in the decrypted payload).
- **Paid unlock (₹249/week at verification time):** a paid session is expected
  to flip `isPaid: true` and return real companies and apply URLs ([INFERENCE] —
  never verified: creating accounts or paying is out of scope for this skill).
- **To enable after legitimate paid access:** sign in on jobs24x.com with a
  paid pass, copy the Supabase session access token, and export it:
  ```bash
  export JOBS24X_API_TOKEN="<supabase access token from your own paid session>"
  ```
  The CLI sends it as the `Authorization: Bearer` header on the same calls the
  site makes. Never commit the token; it lives only in the environment. Then
  set `enabled: true` in this file's frontmatter so `/scrape` picks the portal
  up. If results stay locked with a token set, the token is expired or the
  pass lapsed — the CLI keeps emitting locked rows rather than failing.

## When to use this skill

- Probe what the free surface returns for a keyword (titles/dates only)
- Look one jobs24x posting up by its UUID (returns the locked record + unlock note)
- After paid access is configured: search with real companies and apply URLs

## Commands

### Search job listings

```bash
bun run .agents/skills/jobs24x-search/cli/src/cli.ts search [-q "<keywords>"] [filters]
```

Key flags:
- `--query <text>` / `-q <text>` — keyword search. Optional.
- `--remote <mode>` — e.g. `Remote`, `On-site`, `Hybrid`.
- `--employment-type <type>` — e.g. `Full-time`, `Contract`.
- `--country <code>` — country filter.
- `--page <n>` — 1-indexed page. Default 1.
- `--limit <n>` / `-n <n>` — results per page. Default 12 (the site's page size).
- `--format json|table|plain` — default `json`.

### Fetch full job detail

```bash
bun run .agents/skills/jobs24x-search/cli/src/cli.ts detail <uuid> [--query "<keywords>"] [--format json|plain]
```

`uuid` is the `id` from a `search` result. list-jobs has no per-job lookup,
so replay the listing query that surfaced it: `detail <uuid> --query "<same
query>"` (plus the same `--remote` / `--employment-type` / `--country` when
the search used them). Walks the filtered listing pages until the job appears
(max 10, via `--max-pages`) and returns the same locked record search would,
plus the unlock note. A UUID that never appears returns `NOT_FOUND`.

## Usage examples

```bash
# Free-surface probe: what titles exist for a keyword
bun run .agents/skills/jobs24x-search/cli/src/cli.ts search -q "python" --limit 5 --format table

# Remote-only filter
bun run .agents/skills/jobs24x-search/cli/src/cli.ts search -q "python" --remote Remote --format table

# Look up one posting (locked record + unlock note); replay the search query
bun run .agents/skills/jobs24x-search/cli/src/cli.ts detail eb7ed263-0eb0-4c0f-ac1b-602be06f98be --query python --format plain
```

## Output formats

| Format | Best for |
|--------|----------|
| `json` | Default — programmatic use |
| `table` | Quick human-readable scanning |
| `plain` | Reading a single job's locked record (`detail` command) |

Search JSON is `{ "meta": { "count", "page", "total", "locked": true }, "results": [...] }`;
each result carries at least `id`, `title`, `company`, `location`, `date`,
`url`, and `description`, plus `locked`. Locked rows have `company`/`url`
`null`. `table` and `plain` mark locked rows `(locked)`. All errors are written
to **stderr** as `{ "error": "...", "code": "..." }` and the process exits with code `1`.

## Notes

- No accounts are ever created and nothing is ever paid for by this skill — it
  only reads what anonymous visitors get.
- `id` is jobs24x's UUID for the posting — pass it as-is to `detail`.
- `date` is the posting date (`date_posted`); it may be `null`.
- The anon key the CLI sends is the site's own **public** client key (shipped
  in its JS bundle for every visitor; Supabase anon keys are public by design
  and gated server-side). It is not a secret and not an account.
- `robots.txt` allows `/` (only `/auth`, `/saved`, `/unsubscribe`,
  `/email-preview` disallowed) — none of which this skill touches.
