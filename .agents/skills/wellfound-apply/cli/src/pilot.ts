import { appendFileSync, existsSync, readFileSync } from "fs";
import { isAbsolute, join } from "path";
import { fail, localDay, readJsonFile } from "./helpers.js";

export const DEFAULT_MAX_PER_DAY = 10;
export const RECEIPTS_FILE = "receipts.jsonl";

export interface QueueEntry {
  job: string;
  company: string;
  role: string;
  url: string;
  status: "queued" | "approved";
  cvPdf: string | null;
  coverPdf: string | null;
  requiredAnswers: string[];
  approvedBy: string | null;
  approvedAt: string | null;
  notes: string;
}

export interface QueueFile {
  version: 1;
  jobs: QueueEntry[];
}

export interface Receipt {
  ts: string;
  job: string;
  url: string;
  mode: "dry-run" | "live";
  documentsSent: { cv: string; cover: string };
  answersGiven: Record<string, string>;
  screenshot: string;
  result: string;
}

export function resolveStateDir(explicit?: string): string {
  return explicit ?? join(".agents", "skills", "wellfound-apply", "pilot");
}

export function loadQueue(stateDir: string): QueueFile {
  const path = join(stateDir, "approved-queue.json");
  const queue = readJsonFile<QueueFile>(path, "approval queue");
  if (!Array.isArray(queue.jobs)) fail(`approval queue at ${path} has no jobs array`, "BAD_ARGS");
  return queue;
}

export function findJob(queue: QueueFile, id: string): QueueEntry {
  const entry = queue.jobs.find((j) => j.job === id);
  if (!entry) fail(`unknown job '${id}' - see 'queue list'`, "UNKNOWN_JOB");
  return entry;
}

export function checkMaxPerDay(raw: string | undefined): number {
  if (raw === undefined) return DEFAULT_MAX_PER_DAY;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 1 || n > DEFAULT_MAX_PER_DAY) {
    fail(`--max-per-day must be an integer 1-${DEFAULT_MAX_PER_DAY} (pilot cap only moves down)`, "BAD_CAP");
  }
  return n;
}

export function countLiveSubmitsToday(stateDir: string, now = new Date()): number {
  const path = join(stateDir, "receipts", RECEIPTS_FILE);
  if (!existsSync(path)) return 0;
  const today = localDay(now.toISOString());
  let count = 0;
  for (const line of readFileSync(path, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const receipt = JSON.parse(trimmed) as Receipt;
      if (receipt.mode === "live" && receipt.result === "submitted" && localDay(receipt.ts) === today) {
        count++;
      }
    } catch {
      // A half-written line from a killed run must not inflate or hide the cap.
    }
  }
  return count;
}

export function loadLockedAnswers(stateDir: string): Record<string, string> {
  const path = join(stateDir, "answers.locked.json");
  if (!existsSync(path)) {
    fail(
      `locked answers file missing at ${path} - copy answers.locked.example.json and have the captain review it`,
      "NEEDS_ANSWER",
    );
  }
  return readJsonFile<Record<string, string>>(path, "locked answers");
}

export function missingAnswers(entry: QueueEntry, locked: Record<string, string>): string[] {
  return entry.requiredAnswers.filter((key) => !(key in locked) || locked[key].trim().length === 0);
}

export function checkSubmittable(entry: QueueEntry, repoRoot = "."): { cv: string; cover: string } {
  if (entry.status !== "approved" || !entry.approvedBy || !entry.approvedAt) {
    fail(`job '${entry.job}' is not approved - captain approval required before submit`, "NOT_APPROVED");
  }
  for (const [label, rel] of [["cv", entry.cvPdf], ["cover letter", entry.coverPdf]] as const) {
    const full = rel && isAbsolute(rel) ? rel : rel ? join(repoRoot, rel) : null;
    if (!full || !existsSync(full)) {
      fail(`job '${entry.job}' ${label} document missing: ${rel ?? "(none recorded)"}`, "MISSING_DOCUMENTS");
    }
  }
  return { cv: entry.cvPdf as string, cover: entry.coverPdf as string };
}

export function sessionStatus(env: NodeJS.ProcessEnv = process.env): { ok: boolean; via: string | null } {
  if (env.CHROME_DEVTOOLS_AXI_BROWSER_URL) return { ok: true, via: "CHROME_DEVTOOLS_AXI_BROWSER_URL" };
  if (env.CHROME_DEVTOOLS_AXI_USER_DATA_DIR) return { ok: true, via: "CHROME_DEVTOOLS_AXI_USER_DATA_DIR" };
  return { ok: false, via: null };
}
export function appendReceipt(stateDir: string, receipt: Receipt): string {
  const path = join(stateDir, "receipts", RECEIPTS_FILE);
  try {
    appendFileSync(path, JSON.stringify(receipt) + "\n");
  } catch {
    fail(`cannot append receipt to ${path}`, "BAD_ARGS");
  }
  return path;
}
