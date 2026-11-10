# LinkedIn dispatch: one batch at a time, on demand

No schedule, no daemon. The operator triggers every batch by hand, watches
it, and checks `status` after. One batch at a time: a live `run`,
`discover`, `login`, or `status --probe` holds the single-writer lock
(`batch.lock` in the state dir) and any second batch refuses until the
first finishes.

## State lives outside the repo

Default state dir: `~/.linkedin-connect-bot/state/` (`ledger.json`,
`audit.jsonl`, `discovery_cache.json`). Every worktree resolves to the
same dir, so disposable checkouts can never reset caps or split the
ledger. `--state-dir` still overrides it (tests use this). The login
profile lives next door at `~/.linkedin-connect-bot/profile/` and is
never committed.

Migrating once from the old in-repo `linkedin_connect/`: the tool
refuses the default and prints the exact `mv` until you move
`ledger.json` (and `audit.jsonl` / `discovery_cache.json` alongside).

## Batch template (fill in, then run)

- **quota for this batch**: at most N invites (`--limit N`, still under
  the 20/day and 90/week hard caps from the durable ledger).
- **queue source**: `--from-ledger` (discover-queued) and/or
  `--candidates queue.json` (hand-collected).
- **live flag**: dry-run first (no `--live`: no browser, nothing written
  except nothing at all for discover; run dry-run only prints). Add
  `--live` only for the real pass.
- **window deadline**: inside `--window` (default 09:00-21:00 local);
  outside it the pass refuses before any browser starts.

## Cold procedure (a fresh worker follows this verbatim)

```bash
python3 tools/linkedin_connect.py status
python3 tools/linkedin_connect.py discover            # dry: what would be queued
python3 tools/linkedin_connect.py discover --live     # only if the queue needs topping up
python3 tools/linkedin_connect.py plan --candidates queue.json   # hand queue scoring
python3 tools/linkedin_connect.py run --from-ledger   # dry: what would be sent
python3 tools/linkedin_connect.py run --from-ledger --live --limit 5
python3 tools/linkedin_connect.py status
```

If any step prints `refused:` or `BOT STOPPED`, stop: read the `Last
error` line from `status`, do not re-run. A `refused: ... lock ...`
means a sibling batch is running - wait for it. A `refused: ... login
...` means the login window is open or the profile was touched outside
the tool - see `tools/LINKEDIN_LOGIN.md`, never force it.

`status --probe` is the only sanctioned live session check, and only
with the explicit flag: one feed load behind the same lock and guard,
read-only.
