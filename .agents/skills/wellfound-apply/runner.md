# Wellfound Apply Pilot — Runner Docs

Captain-approved submits on Wellfound, nothing else. Read this whole file
before running `submit`. The CLI enforces every gate below; this doc tells you
the order to run things in and what to do when a gate fires.

## 0. One-time setup

```bash
# From the repo root. No installs: the CLI is zero-dependency (bun only).
cp .agents/skills/wellfound-apply/pilot/answers.locked.example.json \
   .agents/skills/wellfound-apply/pilot/answers.locked.json
```

Open `pilot/answers.locked.json`, fill in the captain's real answers, and have
the captain review it. `answers.locked.json` is gitignored and never committed;
only the `.example.json` template ships. Re-review the file whenever an answer
changes — `submit` trusts it verbatim.

## 1. Session handoff (captain's browser, your commands)

The captain, in their own Chrome, logs into wellfound.com. Passwords, OTPs,
and magic links never leave the captain's hands — the CLI has no flag, env
var, or prompt that accepts them.

The captain then gives you **exactly one** of:

| Variable | Meaning |
|---|---|
| `CHROME_DEVTOOLS_AXI_BROWSER_URL` | Attach to the captain's running Chrome, e.g. `http://127.0.0.1:9222` |
| `CHROME_DEVTOOLS_AXI_USER_DATA_DIR` | Launch bound to the captain's profile directory |

Export it and confirm the session before anything else:

```bash
export CHROME_DEVTOOLS_AXI_BROWSER_URL=http://127.0.0.1:9222
bun run .agents/skills/wellfound-apply/cli/src/cli.ts status
```

If the session is missing, `status` still works (it reports
`session: "missing"`), but `submit` exits 1 with `MISSING_SESSION` before
opening any page. Never work around this by logging in yourself, using a
different account, or scripting credentials.

## 2. Approval queue

`pilot/approved-queue.json` ships with the two pilot roles in `queued` state.
A job becomes submittable only through an explicit captain approval with
`/apply`-tailored documents already on disk:

```bash
# Captain: review CV + cover letter first, then approve.
bun run .agents/skills/wellfound-apply/cli/src/cli.ts queue approve \
  --job certa-sdet-india \
  --cv cv/main_certa_sdet.pdf \
  --cover cover_letters/cover_certa_sdet.pdf \
  --by "Captain"
```

`queue approve` refuses missing document paths (`MISSING_DOCUMENTS`), and
`submit` re-checks them at run time. To pull a job back out of the submit
path, set its `status` to `"queued"` in the queue file.

List the queue any time:

```bash
bun run .agents/skills/wellfound-apply/cli/src/cli.ts queue list --format table
```

## 3. Screening answers

Each queue entry lists `requiredAnswers`: keys into `answers.locked.json`
(e.g. `work_authorization_in`, `notice_period_days`). Check coverage before
booking a run:

```bash
bun run .agents/skills/wellfound-apply/cli/src/cli.ts answers check --job certa-sdet-india
```

Missing keys exit 1 with `NEEDS_ANSWER` naming the key. Add the answer to the
locked file, get the captain's review, re-run the check. The runner never
invents, rephrases-into-new-facts, or reuses a near-match key.

## 4. Dry-run first (default), live only on order

```bash
# Dry-run: stages everything, screenshots, stops before the final click.
bun run .agents/skills/wellfound-apply/cli/src/cli.ts submit --job certa-sdet-india

# Live: only with an explicit captain go-order for this job.
bun run .agents/skills/wellfound-apply/cli/src/cli.ts submit --job certa-sdet-india --live
```

The dry-run walkthrough per job:

1. Attach to the captain's session (fail closed: `MISSING_SESSION`).
2. Open the posting URL; confirm the company/role still matches the queue
   entry. On mismatch, stop (`NOT_APPROVED` — the approval was for a
   different posting).
3. Open the apply form. On an external-ATS redirect, stop: record
   `result: "external-ats"` with the redirect URL, never follow it.
4. Stage `cvPdf` + `coverPdf` uploads and fill each screening field from the
   locked answers only. A field with no locked key stops the run
   (`NEEDS_ANSWER`).
5. Screenshot the staged form to
   `pilot/receipts/<job>-<timestamp>-dry-run.png`.
6. Dry-run stops here. `--live` clicks submit, waits for the confirmation
   state, screenshots it (`-live.png`), then appends the receipt.

## 5. Caps

Max 10 live submits per calendar day (local time), counted from
`pilot/receipts/receipts.jsonl`. Lower it per run with
`--max-per-day 1..10`; anything above 10 is rejected (`BAD_CAP`) — the pilot
cap only moves down. A run past the cap exits 1 (`DAILY_CAP`) before opening
the browser. Dry-runs never count against the cap.

## 6. Receipts

Every `submit` run appends one JSON line to
`pilot/receipts/receipts.jsonl`:

```json
{"ts":"2026-10-29T12:00:00+05:30","job":"certa-sdet-india",
 "url":"https://wellfound.com/jobs/2894265-software-development-engineer-in-test-sdet",
 "mode":"dry-run","documentsSent":{"cv":"cv/main_certa_sdet.pdf","cover":"cover_letters/cover_certa_sdet.pdf"},
 "answersGiven":{"work_authorization_in":"...","notice_period_days":"..."},
 "screenshot":"pilot/receipts/certa-sdet-india-20261029T120000-dry-run.png",
 "result":"staged"}
```

`result` is one of `staged` (dry-run), `submitted` (live, confirmation seen),
`external-ats`, or `aborted:<CODE>`. Receipts, screenshots, and the locked
answers file are gitignored runtime state.

## 7. Pilot proof (two queued roles)

| Job id | Posting | State at build |
|---|---|---|
| `certa-sdet-india` | Certa, SDET, India remote — https://wellfound.com/jobs/2894265-software-development-engineer-in-test-sdet | queued |
| `certa-tpm-us` | Certa, Technical Project Manager, US remote — https://wellfound.com/jobs/4762770-technical-project-manager | queued |

Both ship `queued`: no approval, no tailored documents, no answers at build
time — and no real application was submitted during the build. The pilot proof
is the gated path (approve → answers check → dry-run → receipt), exercised
end to end in dry-run, not a live submit.
