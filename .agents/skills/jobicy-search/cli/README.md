# jobicy-cli

CLI for searching [Jobicy](https://jobicy.com)'s public remote-jobs API.
Zero runtime dependencies — runs anywhere `bun` is available.

## Commands

```bash
# Search (server-side tag filter + client-side keyword refinement)
bun run src/cli.ts search --tag python --limit 10 --format table

# Geography + industry filters
bun run src/cli.ts search --geo usa --industry engineering

# Detail by id, slug, or posting URL (includes live open/closed status)
bun run src/cli.ts detail 161642 --format plain
```

## Notes

- The feed covers the last 7 days; cursor pagination implements `--page`.
- Jobicy asks that it be credited with a direct link — result `url` values are
  the canonical Jobicy posting URLs, and applications go through them.
