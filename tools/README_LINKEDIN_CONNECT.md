# LinkedIn Connect Bot

Sends LinkedIn connection requests to hiring people relevant to the search
(recruiters, hiring managers, engineering leaders in GenAI/AI engineering) —
fully automatic once flipped live, capped at 20 invites per day.

> ⚠️ **Personal use only, and a ToS warning you must read.** Automated
> connection requests violate LinkedIn's Terms of Service. LinkedIn detects
> automation and restricts or bans accounts that use it. This tool exists
> because the operator was warned of exactly that and explicitly accepted
> the risk. Keep volume low, never use it commercially, and never run it
> from CI or on a schedule. If that paragraph makes you uncomfortable, do
> not flip it live — `plan` and dry-run `run` are useful on their own.

## How it works

You hand it a queue of LinkedIn people URLs you collected yourself (open
profiles in your browser, save the URLs — the bot never scrapes
people-search pages). It scores each one against
[`linkedin_connect_targets.json`](linkedin_connect_targets.json) — role
signals (recruiter / hiring-manager / eng-leader), GenAI domain signals,
and a remote/NCR location lens — and invites the matches.

```bash
# 1. Score the queue. Writes nothing, sends nothing.
python3 tools/linkedin_connect.py plan --candidates queue.json

# 2. Dry-run. Prints exactly what WOULD be sent, sends nothing,
#    no browser starts, the ledger is untouched. This is the default.
python3 tools/linkedin_connect.py run --candidates queue.json

# 3. One-time setup for live sending (needs Playwright, below):
#    opens a browser; you sign in by hand, then close it.
python3 tools/linkedin_connect.py login

# 4. Live. The explicit operator flag; nothing else sends.
python3 tools/linkedin_connect.py run --candidates queue.json --live

# Quota and history at any time:
python3 tools/linkedin_connect.py status
```

`queue.json` is an array of
`{"profile_url": "...", "name": "...", "headline": "...", "location": "...", "company": "..."}`.
Only `profile_url` is required.

## Safety rails (enforced in code, pinned by `tests/test_linkedin_connect.py`)

| Rail | How |
|------|-----|
| Hard daily cap of 20 | Counted from `linkedin_connect/ledger.json`, which survives restarts. `--max-per-day` can only lower it; anything above 20 is refused. |
| Dry-run default | Without `--live` no browser starts and the ledger is untouched. |
| Instant full stop, exit 2 | Any login challenge, captcha, verification prompt, rate-limit response, or unexpected UI aborts the run with no retry and a loud stderr report. |
| Human-like pacing + window | Jittered 60–180 s pause between invites (tunable down, never off); sending outside `--window` (default 09:00–21:00 local) is refused. |
| Append-only audit log | `linkedin_connect/audit.jsonl`: who, why they matched, timestamp, result. Never rewritten. |
| No credentials in repo/logs | There is no `--password`/`--token` flag at all. Login state lives in a persistent browser profile outside the repo (`~/.linkedin-connect-bot/`). A scrubber redacts credential-looking text before it can reach logs. |
| Dedupe | Ledger statuses `invited / connected / declined / withdrawn` are never re-invited or re-queued, across restarts. |

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
