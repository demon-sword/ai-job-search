# LinkedIn login: the one-window ceremony and how a login stays alive

The login is a persistent Chromium profile at
`~/.linkedin-connect-bot/profile/`. LinkedIn kills sessions that look
automated, and the fastest way to look automated is two browsers (or a
human-driven Chrome plus the bot) holding that one profile at once:
they fight over the cookie store and the lock files, fingerprints
shift, and a minutes-old login dies overnight. So: **one window at a
time, always.**

## The ceremony (run `login`, nothing else, until it returns)

```bash
python3 tools/linkedin_connect.py login
```

1. Close every Chrome/Chromium window on this machine first - including
   one you opened "just to look". Check no batch is running (`status`
   shows no lock refusal; `login` refuses while a batch holds the
   state-dir lock).
2. `login` writes the `login.open` marker: every automated load
   (`run --live`, `discover --live`, `status --probe`) refuses until
   the marker is gone.
3. Sign in by hand in the opened window, 2FA included. Wait until the
   feed is visibly signed in.
4. Close the browser window, then press Enter. `login` clears the
   marker and records the cookie baseline (`profile_sentinel.json`).

If `login` itself is refused on the lock, a batch is mid-flight: let
it finish, then start the ceremony. Never open the profile in a
regular Chrome while a batch runs.

## What keeps a login alive for days

- **Never touch the profile outside the tool.** No manual Chrome on
  that dir, no second checkout running a batch, no "quick check".
  Every guarded load compares the cookie mtime against the noted
  baseline; an outside touch refuses the next batch instead of
  burning the session - re-run the ceremony to re-verify.
- **One batch at a time.** The state-dir lock serializes batches even
  across worktrees, because all of them share the default state dir.
- **Small, human hours.** Default window 09:00-21:00 local, jittered
  60-180 s pacing, 20/day and 90/week hard caps. Volume is what gets
  accounts restricted.
- **Read liveness from `status`, not from loads.** `Session: live`
  means the cookie store was touched recently; `stale` means it sat
  untouched for 7+ days - re-run the ceremony before any batch. Only
  `status --probe` performs a live check, and only when you pass the
  explicit flag.
- **A dead session is a stop, not a retry.** Auth wall, captcha,
  verification, or rate limit halts the batch at once (exit 2) and
  the `Last error` line in `status` says which. Re-run the ceremony,
  then dispatch the next batch - never hammer the dead session.
