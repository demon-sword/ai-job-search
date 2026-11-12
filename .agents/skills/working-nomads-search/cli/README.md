# working-nomads-cli

CLI for searching [Working Nomads](https://www.workingnomads.com)' public jobs
feed (`/api/exposed_jobs/`). Zero runtime dependencies — runs anywhere `bun`
is available.

## Commands

```bash
# Search (keyword across title, company, category, tags, location, description)
bun run src/cli.ts search -q "python" --limit 10 --format table

# Category + recency filter
bun run src/cli.ts search --category Development --jobage 14

# Detail by numeric id or /job/go/<id>/ URL
bun run src/cli.ts detail 2016531 --format plain
```

## Notes

- The feed arrives in one call; all filtering is client-side.
- The authenticated `/api/jobs/` search is deliberately unused (no accounts).
