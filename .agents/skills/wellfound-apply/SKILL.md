---
name: wellfound-apply
version: 1.0.0
description: >
  Use this skill when the user wants to submit a job application on Wellfound
  (wellfound.com, formerly AngelList Talent) through the captain-approved pilot:
  attach to the captain's logged-in browser profile, work an explicit approval
  queue, answer screening questions only from the locked answers file, respect
  the daily submit cap, and write a receipt per submit. Dry-run is the default;
  nothing is submitted without --live. Trigger phrases: apply on Wellfound,
  Wellfound application, submit Wellfound application, Wellfound pilot queue,
  approve Wellfound job, Wellfound receipts.
context: fork
enabled: true
allowed-tools: Bash(bun run .agents/skills/wellfound-apply/cli/src/cli.ts *)
---

# Wellfound Apply Pilot Skill

Submit job applications on **[Wellfound](https://wellfound.com)** (startup job
board, formerly AngelList Talent) through a captain-approved pilot queue. The
captain logs into Wellfound in their own browser profile; the runner attaches
to that profile and never stores or handles passwords. A job enters the submit
path only from an explicit approved list with a CV and cover letter already
tailored by the `/apply` workflow. Screening answers come from a locked answers
file the captain reviews — never invented per submit. Every submit appends a
receipt (timestamp, job URL, documents sent, answers given, confirmation
screenshot path). Dry-run mode does everything except the final submit click
and is **on by default**.

> LinkedIn automation is explicitly out of scope for this skill and must not
> be built here. This skill targets Wellfound only.

## Safety rules (non-negotiable)

1. **No credentials.** The CLI never accepts, stores, or transmits a password
   or OTP. It attaches to the captain's already-logged-in profile via
   `CHROME_DEVTOOLS_AXI_BROWSER_URL` or `CHROME_DEVTOOLS_AXI_USER_DATA_DIR`.
   If neither is set, every browser command fails closed with
   `MISSING_SESSION` before touching Wellfound.
2. **No approval, no submit.** `submit` accepts only a job whose queue entry
   has `status: "approved"` plus existing `cvPdf` and `coverPdf` paths.
   Anything else exits 1 (`NOT_APPROVED`, `MISSING_DOCUMENTS`).
3. **No invented answers.** A screening answer must come from
   `pilot/answers.locked.json`. A question with no locked answer aborts that
   job with `NEEDS_ANSWER`. The runner never improvises a response.
4. **Daily cap.** Default max 10 submits per calendar day (local time),
   configurable **down** with `--max-per-day 1..10`. Values above 10 are
   rejected. Live submits beyond the cap exit 1 (`DAILY_CAP`).
5. **Dry-run default.** Without `--live`, the runner walks the whole flow —
   open posting, open apply form, stage documents and answers, screenshot —
   and stops before the final submit click. Only `--live` clicks it.
6. **Receipts always.** Every `submit` run (dry-run or live) appends one JSON
   line to `pilot/receipts/receipts.jsonl`. No receipt, no submit.

## Commands

All commands run from the repo root. `--state-dir` defaults to
`.agents/skills/wellfound-apply/pilot`.

```bash
bun run .agents/skills/wellfound-apply/cli/src/cli.ts queue list [--format json|table]
bun run .agents/skills/wellfound-apply/cli/src/cli.ts queue approve --job <id> --cv <pdf> --cover <pdf> --by "<name>"
bun run .agents/skills/wellfound-apply/cli/src/cli.ts answers check --job <id>
bun run .agents/skills/wellfound-apply/cli/src/cli.ts status [--format json|table]
bun run .agents/skills/wellfound-apply/cli/src/cli.ts submit --job <id> [--live] [--max-per-day <1-10>]
```

Key flags:

- `submit --live` — perform the final submit click. Omit for dry-run
  (default). Dry-run still requires an approved job, staged documents, and
  locked answers; it only skips the last click.
- `submit --max-per-day <n>` — daily live-submit cap, integer 1–10.
  Default 10. `--max-per-day 11` exits 1 (`BAD_CAP`).
- `--format json|table` — `queue list` and `status` default to `json`;
  `table` is the human-scanning view.

## Browser session handoff

The captain, in their own Chrome:

1. Logs into wellfound.com normally.
2. Enables remote debugging (or shares the profile directory) and gives the
   runner exactly one of:
   - `CHROME_DEVTOOLS_AXI_BROWSER_URL=http://127.0.0.1:9222` (attach), or
   - `CHROME_DEVTOOLS_AXI_USER_DATA_DIR=/path/to/profile` (launch on copy).
3. The runner exports that variable, then runs `status` to confirm the
   session before any `submit`.

Full step-by-step, including the dry-run walkthrough and what to do when a
form field has no locked answer, is in `runner.md`.

## Output and errors

- `queue list` / `status` print JSON by default (`{ "meta": ..., "results"/... }`).
- `submit` prints the run plan and result as JSON on stdout.
- All errors go to **stderr** as `{ "error": "...", "code": "..." }` with
  exit code `1`. Codes: `MISSING_SESSION`, `NOT_APPROVED`,
  `MISSING_DOCUMENTS`, `NEEDS_ANSWER`, `DAILY_CAP`, `BAD_CAP`,
  `UNKNOWN_JOB`, `UNKNOWN_FLAG`, `BAD_ARGS`.

## Notes

- Wellfound apply forms vary per company (native flow vs. external ATS
  redirect). On an external redirect the runner stops, records
  `result: "external-ats"` in the receipt with the redirect URL, and never
  follows the posting into a third-party application.
- `queue approve` records who approved and when (`approvedBy`, `approvedAt`);
  re-approving overwrites the approval. Removing a job from the submit path
  is a queue-file edit (set `status` back to `"queued"`).
- Receipt screenshots and `receipts.jsonl` are gitignored runtime state;
  `answers.locked.json` is gitignored too — only
  `answers.locked.example.json` and the URL-only `approved-queue.json` ship.
