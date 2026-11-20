# LinkedIn Connect Bot

Finds hiring people relevant to the search (recruiters, hiring managers,
engineering leaders in GenAI/AI engineering) and sends them connection
requests — fully automatic once flipped live, capped at 20 invites per day.

> ⚠️ **Personal use only, and a ToS warning you must read.** Automated
> connection requests violate LinkedIn's Terms of Service. LinkedIn detects
> automation and restricts or bans accounts that use it. This tool exists
> because the operator was warned of exactly that and explicitly accepted
> the risk. Keep volume low, never use it commercially, and never run it
> from CI or on a schedule. If that paragraph makes you uncomfortable, do
> not flip it live — `plan` and dry runs are useful on their own.
>
> ⚠️ **Discovery raises the risk further.** People-search scraping and
> profile views are heavier automation signals than occasional invites:
> they burn through search-result quotas and trip rate limits faster. The
> discovery budgets default low (3 searches, 2 pages each, 5 profile
> views, 20 queued/day) for exactly this reason — raising them raises the
> odds of a restriction, not just the yield.

## How it works

Two stages share one ledger, one audit log, and one safety discipline:

1. **Discover** searches LinkedIn people search with seeded keywords —
   role-title seeds (`recruiter`, `hiring manager`, `engineering manager`,
   …) combined with the GenAI domain terms and location lens from
   [`linkedin_connect_targets.json`](linkedin_connect_targets.json) — scores
   each result card with the same matcher, and queues matches in the ledger
   (`status: queued`, at most 20 new profiles per day). No company-list
   mining, no pasted URLs: the seeds in the targets file are the only
   discovery source.
2. **Invite** sends connection requests to the queued profiles (or to a
   hand-collected `queue.json`, which still works as before).

```bash
# 1. Preview discovery. Reads the cache, prints what WOULD be searched and
#    WOULD be queued. Writes nothing at all. This is the default.
python3 tools/linkedin_connect.py discover

# 2. Live discovery: search (within budget), cache raw pages, queue matches.
python3 tools/linkedin_connect.py discover --live

# 3. Score a hand-collected queue instead. Writes nothing, sends nothing.
python3 tools/linkedin_connect.py plan --candidates queue.json

# 4. Dry-run invites. Prints exactly what WOULD be sent, sends nothing,
#    no browser starts, the ledger is untouched. This is the default.
python3 tools/linkedin_connect.py run --from-ledger

# 5. One-window login ceremony (needs Playwright, below):
#    one browser window, sign in by hand, close it. Full rite in
#    `tools/LINKEDIN_LOGIN.md`.
python3 tools/linkedin_connect.py login

# 6. Live. The explicit operator flag; nothing else sends.
#    Dispatch one batch at a time, on demand - see `tools/LINKEDIN_DISPATCH.md`.
python3 tools/linkedin_connect.py run --from-ledger --live
# ...or the hand-collected queue, as before:
python3 tools/linkedin_connect.py run --candidates queue.json --live

# Quota, last error, and session liveness at any time (plain words,
# no browser starts; --json for the machine shape):
python3 tools/linkedin_connect.py status
```

`queue.json` is an array of
`{"profile_url": "...", "name": "...", "headline": "...", "location": "...", "company": "..."}`.
`profile_url` and the rendered target's full `name` are required for live
invites. `run --from-ledger` composes with `--candidates`.

## Safety rails (enforced in code, pinned by `tests/test_linkedin_connect.py`)

| Rail | How |
|------|-----|
| Hard daily cap of 20 | Confirmed sends plus unresolved attempt reservations in the authoritative, fsynced `~/.linkedin-connect-bot/state/ledger.json`. `--max-per-day` can only lower it. |
| Hard weekly cap of 90 | The same accounting over seven calendar dates. Reservations consume quota until explicitly reconciled; no flag raises the cap. |
| On-demand dispatch, one batch at a time | Live operations and reconciliation hold the canonical state-dir lock; ledger loads and commits occur while it is held. No scheduling changes. |
| Dry-run default | Without `--live`, no browser starts and the ledger is untouched. Discovery dry-run remains fully read-only. |
| Instant full stop | Captcha, verification, restrictions, unexpected UI and uncertain sends stop the batch. Only a plain login wall may enter the existing manual-login grace. |
| Candidate-bound transitions | Connect/More controls belong to the verified target card, not suggested profiles. Direct Connect may return a positive receipt without a modal. Modal Send requires a positive target receipt; disappearance/click completion is not confirmation. A potentially sending press is never retried. |
| Stable login | Every automated browser load is guarded: no loads while the one-window login ceremony is open, no loads when the profile cookies changed outside the tool since the last guarded load (a second browser on the live profile is the overnight killer). Refusals name the recovery: finish or re-run the ceremony (`tools/LINKEDIN_LOGIN.md`). |
| Queue order by invitable-confidence | Direct-Connect profiles first, More-overflow second, unknown variants last — scored from cached data only (the `entry` recorded on discovery profile views, prior `follow_only`/`note_required` outcomes), never from extra page visits. Stable within a tier. Hand-supplied `--candidates` files may carry `"entry": "direct" | "more" | "unknown"`. |
| Human-like pacing + window | Jittered 60–180 s pacing with a durable deadline renewed for possible side effects and completed outcomes; the CLI refuses less than 60s. Discovery's existing 8–20 s pacing and 09:00–21:00 window are unchanged. |
| Discovery budgets | At most 3 seeded searches, 2 pages each, 5 profile views per run (all tunable down in the targets file). Raw pages are cached in `<state-dir>/discovery_cache.json` so a re-run re-scores from disk and never re-scrapes. |
| Durable outcomes and audit | Ledger reservations precede possible side effects; each result is persisted immediately. Audit is append-only diagnostic history, not a competing counter. `status` exposes interruption, reservations and unresolved profiles. |
| No credentials in repo/logs | There is no `--password`/`--token` flag at all. Login state lives in a persistent browser profile outside the repo (`~/.linkedin-connect-bot/`). A scrubber redacts credential-looking text before it can reach logs. |
| Dedupe and reconciliation | Invited/connected/terminal and unresolved targets cannot be resent. An unresolved attempt blocks live passes. Explicit, evidence-backed `reconcile` previews by default, backs up before `--apply`, and refuses conflicting replay. See `LINKEDIN_DISPATCH.md`. |

The live sender waits for the exact canonical URL and one target heading
inside its structural profile section, with that section's own action
group. Nested identity/Contact-info fragments are not action-card roots.
Sidebar, activity and sticky duplicate controls cannot supply Connect,
More, Pending, degree or Follow classifications. Accessible target identity
remains required. An unsupported boundary, hydration timeout or inaccessible
More is diagnostic unknown, never a sticky page-wide `follow_only`.

Every pre-action failure is captured before closing the page: sanitized
final URL/readiness and heading/root/control counts, screenshot, and the
complete minimal structural card DOM. Captures are local operational
evidence, not permission to retry. No change to positive acknowledgements,
durable reservations, interruption recovery, quotas or pacing is implied.

Pending recognition includes LinkedIn's exact `Pending, click to withdraw
invitation sent to <full target name>` accessible label, only in the
verified target action group. It is read-only status evidence, never a
Withdraw action. A first-name-only success toast does not confirm delivery.

Overflow entry reads ignore hidden copies. Resolution accepts the exact
accessible label or exact visible text inside one visible dropdown; it
never picks the first of ambiguous entries/menus. Wrong-person labels,
disabled entries and destructive/note-only text cannot supply Connect.
The non-sending keyboard fallback closes an existing menu before reopening
it; no fallback occurs after a possibly sending press.

The Nov-21 Vinaayak capture retained a named invitation entry and visible
Connect, but not its menu DOM. Offline regressions on the captured profile
section cover both accessible-name/text mismatch and hidden-name duplicates;
these are counterfactual menu layouts, not a proven unique live cause.
The permitted read-only revisit found no mounted menu. Exact-layout live
validation still needs a separately authorized non-sending capture of that
target's open More menu; no invitation/retry is implied.

## Setup for live sending

```bash
pip install playwright
python -m playwright install chromium
python3 tools/linkedin_connect.py login   # sign in by hand once
```

The profile directory (`~/.linkedin-connect-bot/profile/`) and the state
dir (`~/.linkedin-connect-bot/state/`) stay on your machine and are never
committed (the legacy in-repo `linkedin_connect/` state is gitignored too).
Tune targeting by copying `tools/linkedin_connect_targets.json` and passing
`--targets` — criteria are data, never hardcoded names. An optional invite
note (≤300 chars) comes from `--note-file`.

Exit codes: `0` ok (including a planned halt at the daily cap),
`1` usage/state/refused (outside window, lock held, login guard, bad flags),
`2` safety stop.
