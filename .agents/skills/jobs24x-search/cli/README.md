# jobs24x-cli

CLI for reading [jobs24x](https://jobs24x.com/jobs)' job listing through the
site's own encrypted edge functions. Zero runtime dependencies — runs anywhere
`bun` is available.

## Status: paywalled free surface

Anonymous calls return **locked rows only** (title/snippet/date, no company or
apply URL). The skill ships `enabled: false` so `/scrape` skips it. See
`SKILL.md` (Auth gap) for the paid-unlock path.

## Commands

```bash
# Free-surface probe (locked rows)
bun run src/cli.ts search -q "python" --limit 5 --format table

# Look up one posting (locked record + unlock note)
bun run src/cli.ts detail <uuid> --format plain

# With a paid session token (your own — never commit it)
JOBS24X_API_TOKEN="<token>" bun run src/cli.ts search -q "python"
```

## Notes

- No accounts are created and nothing is paid for — anonymous reads only,
  unless the user supplies their own `JOBS24X_API_TOKEN`.
- The anon key sent with requests is the site's public client key (shipped in
  its JS bundle for every visitor), not a secret.
