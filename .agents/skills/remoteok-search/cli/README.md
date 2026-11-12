# remoteok-cli

CLI for searching [RemoteOK](https://remoteok.com)'s public remote-jobs API.
Zero runtime dependencies — runs anywhere `bun` is available.

## Commands

```bash
# Search (keyword across position, company, tags, location, description)
bun run src/cli.ts search -q "python" --limit 10 --format table

# Recent roles only
bun run src/cli.ts search --jobage 14

# Detail by id, slug, or posting URL
bun run src/cli.ts detail 1137714 --format plain
```

## Notes

- The API returns the whole feed in one call; all filtering is client-side.
- RemoteOK's terms require linking back to the posting URL and mentioning
  RemoteOK as a source — result `url` values are the canonical posting URLs.
