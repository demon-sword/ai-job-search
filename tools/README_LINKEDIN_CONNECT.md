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

# 5. One-time setup for live sending (needs Playwright, below):
#    opens a browser; you sign in by hand, then close it.
python3 tools/linkedin_connect.py login

# 6. Live. The explicit operator flag; nothing else sends.
python3 tools/linkedin_connect.py run --from-ledger --live
# ...or the hand-collected queue, as before:
python3 tools/linkedin_connect.py run --candidates queue.json --live

# Quota and history at any time:
python3 tools/linkedin_connect.py status
```

`queue.json` is an array of
`{"profile_url": "...", "name": "...", "headline": "...", "location": "...", "company": "..."}`.
Only `profile_url` is required. `run --from-ledger` invites the
discover-queued profiles instead (and composes with `--candidates`).

## Safety rails (enforced in code, pinned by `tests/test_linkedin_connect.py`)

| Rail | How |
|------|-----|
| Hard daily cap of 20 | Counted from `linkedin_connect/ledger.json`, which survives restarts. `--max-per-day` can only lower it; anything above 20 is refused. Discovery queues at most 20 new profiles per day too (`--max-queued`, same ceiling). A pass that would start at or over either cap is refused before any browser starts. |
| Hard weekly cap of 90 | Rolling 7-day window over the same send history, absolute (no flag raises it). The pass refuses to start at 90 and halts mid-loop when the 90th send lands. `status` reports `sent_last_7d` / `remaining_week`. |
| Dry-run default | Without `--live` no browser starts and the ledger is untouched. Discovery dry-run is fully read-only: it scores the cache and writes nothing — no cache, ledger, or audit entries. |
| Instant full stop, exit 2 | Any login challenge, captcha, verification prompt, rate-limit response, or unexpected UI aborts the run with no retry and a loud stderr report. Discovery applies the same classifier to every search and profile page. Invite entry is scan-then-verify: page-wide control names are partitioned (Connect candidates, Follow flag, More indices) and every click must open a dialog addressing the candidate by name before Send, so a suggested profile's controls can never invite a stranger. |
| Self-verifying transitions | Before any entry control is touched, a page-covering obstruction — dialog overlay or role-less ad lightbox (fullscreen-fixed scrim with a close control) — is dismissed via Escape then one close/decline click on the exact node, never an accept, with before/after captures; re-checked before every More expand, and an obstruction seen after two clears skips as `overlay_blocked`. A visible top-level Connect (button, link, or ARIA button alike, text-matched so non-`<button>` controls are seen) is always preferred; More → Connect only when none exists or verifies. After expanding More, the menu read must be non-empty and contain the verified entry before clicking (one bounded re-expand + re-read per More); after clicking Connect, the dialog must verify before sending (one bounded re-press, then one keyboard fallback). Every attempt saves a screenshot + DOM snippet under `linkedin_connect/diag_run/` (gitignored), win or lose. Step misses skip as `menu_missed` / `dialog_missed` / `overlay_blocked` without a ledger write (max 3 consecutive, 4th stops loud). |
| Queue order by invitable-confidence | Direct-Connect profiles first, More-overflow second, unknown variants last — scored from cached data only (the `entry` recorded on discovery profile views, prior `follow_only`/`note_required` outcomes), never from extra page visits. Stable within a tier. Hand-supplied `--candidates` files may carry `"entry": "direct" | "more" | "unknown"`. |
| Human-like pacing + window | Jittered 60–180 s pause between invites (tunable down, never off); jittered 8–20 s pause between discovery fetches; anything outside `--window` (default 09:00–21:00 local) is refused. |
| Discovery budgets | At most 3 seeded searches, 2 pages each, 5 profile views per run (all tunable down in the targets file). Raw pages are cached in `linkedin_connect/discovery_cache.json` so a re-run re-scores from disk and never re-scrapes. |
| Append-only audit log | `linkedin_connect/audit.jsonl`: who, why they matched, timestamp, result. Never rewritten. |
| No credentials in repo/logs | There is no `--password`/`--token` flag at all. Login state lives in a persistent browser profile outside the repo (`~/.linkedin-connect-bot/`). A scrubber redacts credential-looking text before it can reach logs. |
| Dedupe | Ledger statuses `invited / connected / declined / withdrawn` — plus discovery's `queued` — are never re-queued or re-invited, across restarts. Follow-only profiles go through More → Connect (a name-verified `Invite <name> to connect` entry, or a unique bare `Connect` inside the opened menu, wins; bare send); forced-note dialogs skip as `note_required` (a note is never written); step misses skip as `menu_missed` / `dialog_missed` / `unknown_controls` without a ledger write (max 3 consecutive, 4th stops loud). |

## Setup for live sending

```bash
pip install playwright
python -m playwright install chromium
python3 tools/linkedin_connect.py login   # sign in by hand once
```

The profile directory stays on your machine and is never committed
(`linkedin_connect/` state is gitignored too). Tune targeting by copying
`tools/linkedin_connect_targets.json` and passing `--targets` — criteria
are data, never hardcoded names. An optional invite note (≤300 chars)
comes from `--note-file`.

Exit codes: `0` ok (including a planned halt at the daily cap),
`1` usage/state/refused-outside-window, `2` safety stop.
