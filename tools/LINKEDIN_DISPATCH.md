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

- **quota for this batch**: at most N processed candidates (`--limit N`),
  under the 20/day and 90/week caps including unresolved reservations.
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

## Interrupted or uncertain invitations

`ledger.json` is the sole accounting authority. It is atomically replaced
and fsynced before possible side effects and after each result. The audit
is append-only history, never replayed as if every old `sent` label were
platform proof. Status reports confirmed sends separately from reserved
quota and identifies interrupted runs. Cookie recency is a heuristic,
not a signed-in verdict.

An interrupted `attempting` or an `uncertain` outcome consumes quota and
blocks live runs and discovery. Never delete the ledger, reservation,
sentinel or lock to resume. Do not repeat Connect/Send to test an outcome.
Inspect preserved target-specific evidence or LinkedIn's Sent invitations
read-only. A screenshot still showing Connect alone is not affirmative
proof of rejection.

Use an operator-reviewed JSON manifest with a stable `id`, non-empty
`reason`, and `records`. Each record has `candidate` (profile_url/name),
`outcome` (`sent`, `uncertain`, `connected`, or `not_sent`), a timezone-aware
`ts`, and `evidence` (paths to preserved files). `not_sent` is an explicit
affirmative resolution of an unresolved attempt, not a timeout fallback;
it permits a later separately authorized retry. Confirmed send history
cannot be erased. An optional `source_status: interrupted` records recovery
of a known interrupted run.

```sh
python3 tools/linkedin_connect.py reconcile --manifest incident.json
python3 tools/linkedin_connect.py reconcile --manifest incident.json --apply
python3 tools/linkedin_connect.py status --json
```

Preview is read-only and never opens a browser. Apply holds the same lock,
backs up ledger/audit/sentinel under `state/backups/<timestamp>-<id>/`,
records original hashes plus evidence hashes, and commits the entire
reconciliation atomically to the ledger. Original audit rows remain;
one reconciliation row is appended. Identical id/content replay is a
no-op, while changed content or evidence under the same id refuses.
Keep the backup and captures. Uncertain records remain reserved and
ineligible for another batch even after importing the manifest.

For a proven target-boundary misclassification, `outcome: unknown_controls`
may explicitly reclassify a matching `follow_only` person/attempt only when
no sending actions or confirmed send were recorded. Supply the actual
evidence timestamp and preserved target-card captures; the same backup,
hashing and replay safeguards apply. This changes capability to unknown/
retryable, not invitation availability or delivery. It never opens a
browser or sends; a later retry still requires separate authorization.

Pre-action failures save sanitized final URL/readiness, heading/root/action
counts, a screenshot and complete structural target-card DOM in `diag_run`.
Unknown hydration, inaccessible More or unsupported/ambiguous identity must
not be inferred as a target invitation restriction from page-wide Follow.

