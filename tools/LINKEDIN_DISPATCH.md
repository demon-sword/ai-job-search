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

## Autonomous batch to a confirmation target

`status` only counts the local ledger. Do not dispatch from its remaining
figures alone. First resolve outstanding reservations and review current
account-wide outgoing invitation evidence, including pending, accepted and
withdrawn invitations. Bounds must cover the execution date and the seven
calendar dates ending on it, including all local sends and reservations
without double-counting. A partial Sent page, relative-time labels without a
coverage boundary, and approximate operator estimates are not clearance.

Keep a private, evidence-backed JSON manifest outside the repository:

| Field | Requirement |
|---|---|
| `complete` | `true` only after reviewing complete account activity for both windows. |
| `observed_at` | Actual observation time in ISO 8601 with a timezone, on the execution date. |
| `used_today_upper`, `used_7d_upper` | Integer conservative upper bounds from the evidence, not guesses or remaining counts. |
| `ledger_sha256` | SHA-256 of the current canonical `ledger.json` after any supported reconciliation. |
| `evidence` | Paths to the preserved source files; relative paths are resolved beside the manifest. |

Review the evidence immediately before launch and exclude any other outgoing
invitation activity until the batch ends. The CLI verifies the date, current
ledger hash, referenced files and room for the whole goal under the unchanged
20/day and 90/seven-date ceilings. It records evidence hashes, does not certify
the source's completeness for you, and never imports estimates as send
timestamps. Stop if any part of the coverage is missing.

```bash
python3 tools/linkedin_connect.py status --json
python3 tools/linkedin_connect.py run --live --goal 5 --account-activity /private/activity.json
python3 tools/linkedin_connect.py status --json
```

One invocation consumes the eligible durable queue, then runs at most one
bounded discovery pass if the pool is exhausted before the goal. It chooses
configured seeds by new eligible cached matches, using the same search,
page, profile-view and daily queue budgets. It never retries a profile
within the run. Already pending/connected and proven pre-action skips do
not fill the goal; a possible send without a positive receipt stops before
any replacement. At most 20 candidate decisions and at most `--goal`
possibly sent invitations are allowed. The batch sends no notes. Both
phases use one guarded browser and one writer lock.

To omit a specific target for this invocation, repeat `--exclude PROFILE`
once per target. `PROFILE` is an `in:<slug>` key (e.g. `in:example-person`)
or a LinkedIn `/in/<slug>` profile URL; URL query strings, fragments, and
slug case do not change the match. Excluded targets are ignored in the initial
queue and during discovery/refill, before any profile visit or send attempt.
This is goal-only and does not modify the targets' history, release holds,
or free quota. Account-activity and uncertain-send stops still apply.

`--goal` requires `--live`; `discover` and `run --from-ledger` still offer
standalone previews. A goal dispatch prints confirmed, uncertain, skipped,
already pending/connected and unattempted queue counts and retains them
under `last_run`. Unattempted means known queue entries, not people who
were never discovered. Exit 0 requires the goal; exit 1 means usage/state
refusal or shortfall; exit 2 means safety stop (including unresolved attempts
or unverified capacity); exit 130 means interruption.
Do not follow a shortfall or stop with a second batch to fill the goal.

## Explicit queue batch template

- Establish verified account-wide capacity as above before using the
  explicit queue mode; its `--limit` is not capacity evidence.
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

When preserved affirmative evidence resolves an existing reservation to
`sent`, use its original attempt timestamp for `ts`, not the reconciliation
time. This converts reserved usage to confirmed usage without freeing a
quota slot or counting the invitation twice. The target's exact long Pending
accessible label is affirmative when its verified action card and full name
match; a first-name toast alone is not. Never press Pending/Withdraw or
Connect/Send again to obtain proof.

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

