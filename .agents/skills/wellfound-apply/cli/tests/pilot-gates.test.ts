import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { join } from "path";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { parseStderr, runCLI } from "./helpers";

const REPO_ROOT = join(import.meta.dir, "..", "..", "..", "..");

function makeState(overrides?: { status?: string; answers?: Record<string, string> }): string {
  const dir = mkdtempSync(join(tmpdir(), "wf-pilot-"));
  mkdirSync(join(dir, "receipts"), { recursive: true });
  writeFileSync(join(dir, "cv.pdf"), "%PDF-1.4 stub");
  writeFileSync(join(dir, "cover.pdf"), "%PDF-1.4 stub");
  writeFileSync(
    join(dir, "approved-queue.json"),
    JSON.stringify({
      version: 1,
      jobs: [
        {
          job: "test-job",
          company: "TestCo",
          role: "Tester",
          url: "https://wellfound.com/jobs/1-test",
          status: overrides?.status ?? "queued",
          cvPdf: join(dir, "cv.pdf"),
          coverPdf: join(dir, "cover.pdf"),
          requiredAnswers: ["notice_period_days"],
          approvedBy: overrides?.status === "approved" ? "Captain" : null,
          approvedAt: overrides?.status === "approved" ? "2026-10-29T00:00:00Z" : null,
          notes: "",
        },
      ],
    }),
  );
  writeFileSync(join(dir, "answers.locked.json"), JSON.stringify(overrides?.answers ?? { notice_period_days: "30" }));
  return dir;
}

let stateDir = "";

beforeEach(() => {
  stateDir = makeState();
});

afterEach(() => {
  rmSync(stateDir, { recursive: true, force: true });
});

describe("queue list", () => {
  test("lists queued jobs as JSON", async () => {
    const res = await runCLI(["queue", "list", "--state-dir", stateDir], { cwd: REPO_ROOT });
    expect(res.exitCode).toBe(0);
    const body = JSON.parse(res.stdout) as { meta: { count: number } };
    expect(body.meta.count).toBe(1);
  });

  test("unknown job exits UNKNOWN_JOB", async () => {
    const res = await runCLI(["answers", "check", "--job", "nope", "--state-dir", stateDir], { cwd: REPO_ROOT });
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("UNKNOWN_JOB");
  });
});

describe("submit gates", () => {
  test("queued job is refused with NOT_APPROVED", async () => {
    const res = await runCLI(["submit", "--job", "test-job", "--state-dir", stateDir], { cwd: REPO_ROOT });
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("NOT_APPROVED");
  });

  test("missing locked answer aborts with NEEDS_ANSWER", async () => {
    rmSync(stateDir, { recursive: true, force: true });
    stateDir = makeState({ status: "approved", answers: {} });
    const res = await runCLI(["submit", "--job", "test-job", "--state-dir", stateDir], { cwd: REPO_ROOT });
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("NEEDS_ANSWER");
  });

  test("approved job without session fails closed with MISSING_SESSION", async () => {
    rmSync(stateDir, { recursive: true, force: true });
    stateDir = makeState({ status: "approved" });
    const res = await runCLI(["submit", "--job", "test-job", "--state-dir", stateDir], { cwd: REPO_ROOT });
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("MISSING_SESSION");
  });

  test("cap above 10 is rejected with BAD_CAP", async () => {
    const res = await runCLI(["submit", "--job", "test-job", "--state-dir", stateDir, "--max-per-day", "11"], {
      cwd: REPO_ROOT,
    });
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("BAD_CAP");
  });

  test("bogus flag exits UNKNOWN_FLAG on stderr", async () => {
    const res = await runCLI(["submit", "--job", "test-job", "--state-dir", stateDir, "--frobnicate", "x"], {
      cwd: REPO_ROOT,
    });
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("UNKNOWN_FLAG");
  });

  test("daily cap blocks the 11th live submit", async () => {
    rmSync(stateDir, { recursive: true, force: true });
    stateDir = makeState({ status: "approved" });
    const today = new Date().toISOString();
    const lines = Array.from(
      { length: 10 },
      (_, i) =>
        JSON.stringify({
          ts: today,
          job: `old-${i}`,
          url: "https://wellfound.com/jobs/1-test",
          mode: "live",
          documentsSent: { cv: "a", cover: "b" },
          answersGiven: {},
          screenshot: "s.png",
          result: "submitted",
        }),
    ).join("\n");
    writeFileSync(join(stateDir, "receipts", "receipts.jsonl"), lines + "\n");
    const res = await runCLI(
      ["submit", "--job", "test-job", "--state-dir", stateDir, "--live", "--max-per-day", "10"],
      { cwd: REPO_ROOT },
    );
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("DAILY_CAP");
  });
});

describe("queue approve", () => {
  test("approves a queued job and records approver", async () => {
    const res = await runCLI(
      [
        "queue",
        "approve",
        "--job",
        "test-job",
        "--cv",
        join(stateDir, "cv.pdf"),
        "--cover",
        join(stateDir, "cover.pdf"),
        "--by",
        "Captain",
        "--state-dir",
        stateDir,
      ],
      { cwd: REPO_ROOT },
    );
    expect(res.exitCode).toBe(0);
    const body = JSON.parse(res.stdout) as { status: string; approvedBy: string };
    expect(body.status).toBe("approved");
    expect(body.approvedBy).toBe("Captain");
  });

  test("refuses approval when documents are missing", async () => {
    const res = await runCLI(
      ["queue", "approve", "--job", "test-job", "--cv", join(stateDir, "nope.pdf"), "--cover", join(stateDir, "cover.pdf"), "--by", "Captain", "--state-dir", stateDir],
      { cwd: REPO_ROOT },
    );
    expect(res.exitCode).toBe(1);
    expect(parseStderr(res).code).toBe("MISSING_DOCUMENTS");
  });
});
