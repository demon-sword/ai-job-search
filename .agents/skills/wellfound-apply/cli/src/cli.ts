#!/usr/bin/env bun
// wellfound-apply CLI: captain-approved Wellfound submits with dry-run default.
// Zero runtime dependencies (bun only). Gates enforced before any browser step:
// approval, documents, locked answers, daily cap, session attach.
import { existsSync } from "fs";
import { join } from "path";
import { fail, stampForFilename, writeError } from "./helpers.js";
import {
  appendReceipt,
  checkMaxPerDay,
  checkSubmittable,
  countLiveSubmitsToday,
  findJob,
  loadLockedAnswers,
  loadQueue,
  missingAnswers,
  resolveStateDir,
  sessionStatus,
  type Receipt,
} from "./pilot.js";
import { buildSubmitPlan, driveSubmit } from "./driver.js";

interface Flags {
  _: string[];
  [k: string]: string | boolean | string[];
}

function parseFlags(argv: string[]): Flags {
  const flags: Flags = { _: [] };
  const alias: Record<string, string> = { n: "limit" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--") || a.startsWith("-")) {
      const key = alias[a.replace(/^-+/, "")] ?? a.replace(/^-+/, "");
      const next = argv[i + 1];
      if (next === undefined || next.startsWith("-")) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      (flags._ as string[]).push(a);
    }
  }
  return flags;
}

function strFlag(flags: Flags, name: string): string | undefined {
  const v = flags[name];
  return typeof v === "string" ? v : undefined;
}

const HELP = `wellfound-apply - captain-approved Wellfound submits (dry-run by default)

USAGE
  bun run src/cli.ts queue list [--format json|table] [--state-dir <dir>]
  bun run src/cli.ts queue approve --job <id> --cv <pdf> --cover <pdf> --by "<name>" [--state-dir <dir>]
  bun run src/cli.ts answers check --job <id> [--state-dir <dir>]
  bun run src/cli.ts status [--format json|table] [--state-dir <dir>]
  bun run src/cli.ts submit --job <id> [--live] [--max-per-day <1-10>] [--state-dir <dir>]

SESSION (submit only)
  CHROME_DEVTOOLS_AXI_BROWSER_URL or CHROME_DEVTOOLS_AXI_USER_DATA_DIR must be
  set to the captain's logged-in profile. The CLI never handles passwords.
`;

function outputFormat(flags: Flags): string {
  const format = strFlag(flags, "format") ?? "json";
  if (format !== "json" && format !== "table") fail(`--format must be json|table`, "BAD_ARGS");
  return format;
}

function toTable(rows: Array<Record<string, string>>, columns: string[]): string {
  const widths = columns.map((c) => Math.max(c.length, ...rows.map((r) => (r[c] ?? "").length)));
  const line = (r: Record<string, string>) => columns.map((c, i) => (r[c] ?? "").padEnd(widths[i])).join("  ");
  return [line(Object.fromEntries(columns.map((c) => [c, c]))), ...rows.map(line)].join("\n");
}

async function cmdQueueList(flags: Flags): Promise<void> {
  const queue = loadQueue(resolveStateDir(strFlag(flags, "state-dir")));
  if (outputFormat(flags) === "table") {
    console.log(
      toTable(
        queue.jobs.map((j) => ({ job: j.job, status: j.status, company: j.company, role: j.role, url: j.url })),
        ["job", "status", "company", "role", "url"],
      ),
    );
    return;
  }
  console.log(JSON.stringify({ meta: { count: queue.jobs.length }, results: queue.jobs }));
}

async function cmdQueueApprove(flags: Flags): Promise<void> {
  const stateDir = resolveStateDir(strFlag(flags, "state-dir"));
  const queuePath = join(stateDir, "approved-queue.json");
  const jobId = strFlag(flags, "job");
  const cv = strFlag(flags, "cv");
  const cover = strFlag(flags, "cover");
  const by = strFlag(flags, "by");
  if (!jobId || !cv || !cover || !by) fail("queue approve needs --job, --cv, --cover, --by", "BAD_ARGS");
  const queue = loadQueue(stateDir);
  const entry = findJob(queue, jobId);
  if (!existsSync(cv as string) || !existsSync(cover as string)) {
    fail(`approval refused: document missing (cv: ${cv}, cover: ${cover})`, "MISSING_DOCUMENTS");
  }
  entry.status = "approved";
  entry.cvPdf = cv;
  entry.coverPdf = cover;
  entry.approvedBy = by;
  entry.approvedAt = new Date().toISOString();
  await Bun.write(queuePath, JSON.stringify(queue, null, 2) + "\n");
  console.log(JSON.stringify({ job: entry.job, status: entry.status, approvedBy: by, approvedAt: entry.approvedAt }));
}

async function cmdAnswersCheck(flags: Flags): Promise<void> {
  const stateDir = resolveStateDir(strFlag(flags, "state-dir"));
  const jobId = strFlag(flags, "job");
  if (!jobId) fail("answers check needs --job", "BAD_ARGS");
  const entry = findJob(loadQueue(stateDir), jobId);
  const locked = loadLockedAnswers(stateDir);
  const missing = missingAnswers(entry, locked);
  if (missing.length > 0) {
    fail(`job '${jobId}' missing locked answers for: ${missing.join(", ")}`, "NEEDS_ANSWER");
  }
  console.log(JSON.stringify({ job: jobId, covered: entry.requiredAnswers }));
}

async function cmdStatus(flags: Flags): Promise<void> {
  const stateDir = resolveStateDir(strFlag(flags, "state-dir"));
  const session = sessionStatus();
  const queue = loadQueue(stateDir);
  const payload = {
    session: session.ok ? `attached (${session.via})` : "missing",
    caps: { maxPerDay: 10, liveSubmitsToday: countLiveSubmitsToday(stateDir) },
    queue: queue.jobs.map((j) => ({ job: j.job, status: j.status, url: j.url })),
  };
  if (outputFormat(flags) === "table") {
    console.log(`session: ${payload.session}`);
    console.log(`live submits today: ${payload.caps.liveSubmitsToday}/${payload.caps.maxPerDay}`);
    console.log(toTable(payload.queue, ["job", "status", "url"]));
    return;
  }
  console.log(JSON.stringify(payload));
}

async function cmdSubmit(flags: Flags): Promise<void> {
  const stateDir = resolveStateDir(strFlag(flags, "state-dir"));
  const jobId = strFlag(flags, "job");
  if (!jobId) fail("submit needs --job", "BAD_ARGS");
  const live = flags["live"] === true || flags["live"] === "true";
  const mode = live ? "live" : "dry-run";
  const maxPerDay = checkMaxPerDay(strFlag(flags, "max-per-day"));
  const queue = loadQueue(stateDir);
  const entry = findJob(queue, jobId);
  const docs = checkSubmittable(entry);
  const locked = loadLockedAnswers(stateDir);
  const missing = missingAnswers(entry, locked);
  if (missing.length > 0) {
    fail(`job '${jobId}' missing locked answers for: ${missing.join(", ")}`, "NEEDS_ANSWER");
  }
  if (live && countLiveSubmitsToday(stateDir) >= maxPerDay) {
    fail(`daily cap reached (${maxPerDay}/day) - pilot cap only moves down`, "DAILY_CAP");
  }
  const session = sessionStatus();
  if (!session.ok) {
    fail(
      "no captain browser session: set CHROME_DEVTOOLS_AXI_BROWSER_URL or CHROME_DEVTOOLS_AXI_USER_DATA_DIR",
      "MISSING_SESSION",
    );
  }

  const stamp = stampForFilename();
  const stagedShot = join(stateDir, "receipts", `${entry.job}-${stamp}-${mode}.png`);
  const confirmShot = join(stateDir, "receipts", `${entry.job}-${stamp}-confirmation.png`);
  const plan = buildSubmitPlan(entry, mode, stagedShot);
  console.log(JSON.stringify({ plan: plan.steps.map((s) => s.action), job: plan.job, mode }));

  const answersGiven: Record<string, string> = Object.fromEntries(entry.requiredAnswers.map((k) => [k, locked[k]]));
  let result = "staged";
  let screenshot = stagedShot;
  try {
    const drove = await driveSubmit(plan, entry, locked, { stagedShot, confirmShot }, process.env);
    result = drove.result;
    screenshot = drove.screenshot;
  } catch (err) {
    const receipt: Receipt = {
      ts: new Date().toISOString(),
      job: entry.job,
      url: entry.url,
      mode,
      documentsSent: { cv: docs.cv, cover: docs.cover },
      answersGiven,
      screenshot,
      result: `aborted:${err instanceof Error ? err.message : "unknown"}`,
    };
    appendReceipt(stateDir, receipt);
    throw err;
  }
  const receipt: Receipt = {
    ts: new Date().toISOString(),
    job: entry.job,
    url: entry.url,
    mode,
    documentsSent: { cv: docs.cv, cover: docs.cover },
    answersGiven,
    screenshot,
    result,
  };
  appendReceipt(stateDir, receipt);
  console.log(JSON.stringify({ job: entry.job, mode, result, screenshot }));
}

const COMMANDS: Record<string, (flags: Flags) => Promise<void>> = {
  "queue list": cmdQueueList,
  "queue approve": cmdQueueApprove,
  "answers check": cmdAnswersCheck,
  status: cmdStatus,
  submit: cmdSubmit,
};

async function main(): Promise<void> {
  const argv = process.argv.slice(2);
  if (argv.length === 0 || argv[0] === "--help" || argv[0] === "-h") {
    console.log(HELP);
    return;
  }
  const flags = parseFlags(argv);
  const positional = flags._ as string[];
  const key = positional[0] === "queue" || positional[0] === "answers" ? `${positional[0]} ${positional[1] ?? ""}` : (positional[0] ?? "");
  const command = COMMANDS[key];
  if (!command) {
    writeError(`unknown command '${positional.join(" ")}' - see --help`, "BAD_ARGS");
    process.exit(1);
  }
  const known = new Set(["format", "state-dir", "job", "cv", "cover", "by", "live", "max-per-day", "_"]);
  for (const token of argv) {
    if (token.startsWith("--")) {
      const name = token.slice(2).split("=")[0];
      if (!known.has(name)) {
        writeError(`unknown flag --${name} for '${key}' - flags are never silently ignored`, "UNKNOWN_FLAG");
        process.exit(1);
      }
    } else if (token.startsWith("-") && token !== "-") {
      const name = token.slice(1).split("=")[0];
      if (name !== "h" && name !== "n" && name !== "limit") {
        writeError(`unknown flag -${name} for '${key}' - flags are never silently ignored`, "UNKNOWN_FLAG");
        process.exit(1);
      }
    }
  }
  await command(flags);
}

await main().catch((err: unknown) => {
  if (err instanceof Error && /BROWSER_STEP|ABORT/.test(err.message)) process.exit(1);
  writeError(err instanceof Error ? err.message : "unexpected failure", "FAILED");
  process.exit(1);
});
