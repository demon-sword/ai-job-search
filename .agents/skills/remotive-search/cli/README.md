# remotive-cli

CLI for searching [Remotive](https://remotive.com)'s public remote-jobs API.
Zero runtime dependencies — runs anywhere `bun` is available.

## Commands

```bash
# Search (keyword across title, company, tags, category, description)
bun run src/cli.ts search -q "backend engineer" --limit 10 --format table

# Filter by category and recency
bun run src/cli.ts search --category software-dev --jobage 14

# Detail by numeric id or posting URL
bun run src/cli.ts detail 2091171 --format plain
```

## Notes

- The API returns the whole feed in one call and ignores server-side
  filtering; all flags are applied client-side (see `url-reference.md`).
- Remotive's API terms require linking back to the posting URL and mentioning
  Remotive as a source — result `url` values are the canonical posting URLs.
