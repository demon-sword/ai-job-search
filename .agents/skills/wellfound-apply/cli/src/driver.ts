import { fail } from "./helpers.js";
import type { QueueEntry } from "./pilot.js";

export interface RunStep {
  action: string;
  detail: string;
}

export interface SubmitPlan {
  job: string;
  url: string;
  mode: "dry-run" | "live";
  steps: RunStep[];
}

export function buildSubmitPlan(entry: QueueEntry, mode: "dry-run" | "live", screenshot: string): SubmitPlan {
  const steps: RunStep[] = [
    { action: "open", detail: entry.url },
    { action: "verify", detail: `posting still shows ${entry.company} / ${entry.role}; on mismatch stop` },
    { action: "click-apply", detail: "open the apply form; on external-ATS redirect stop and record external-ats" },
    { action: "upload-cv", detail: entry.cvPdf as string },
    { action: "upload-cover", detail: entry.coverPdf as string },
    ...entry.requiredAnswers.map((key) => ({ action: "fill", detail: `screening field for locked answer '${key}'` })),
    { action: "screenshot", detail: screenshot },
  ];
  if (mode === "live") {
    steps.push({ action: "click-submit", detail: "final submit click (live only)" });
    steps.push({ action: "screenshot-confirmation", detail: "confirmation state after submit" });
  }
  return { job: entry.job, url: entry.url, mode, steps };
}

export async function runAxi(args: string[], env: NodeJS.ProcessEnv): Promise<string> {
  const proc = Bun.spawn(["chrome-devtools-axi", ...args], {
    stdout: "pipe",
    stderr: "pipe",
    env: { ...env },
  });
  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
    proc.exited,
  ]);
  if (exitCode !== 0) {
    fail(`browser step 'chrome-devtools-axi ${args[0]}' failed: ${stderr.trim().slice(0, 300)}`, "BROWSER_STEP");
  }
  return stdout.trim();
}

export function resolveRef(snapshot: string, target: string): string {
  // Snapshot lines carry refs like @e12 next to the accessible name. A
  // target must resolve to exactly one ref; zero or several aborts the run
  // instead of guessing which element to drive.
  const needle = target.toLowerCase();
  const hits = new Set<string>();
  for (const line of snapshot.split("\n")) {
    if (line.toLowerCase().includes(needle)) {
      for (const match of line.matchAll(/@([A-Za-z0-9_-]+)/g)) hits.add(match[1]);
    }
  }
  if (hits.size !== 1) {
    fail(
      `cannot uniquely resolve '${target}' on the page (${hits.size} candidates) - refusing to guess`,
      "BROWSER_STEP",
    );
  }
  return [...hits][0];
}

export function isExternalAts(snapshot: string): boolean {
  return /ats|greenhouse|lever\.co|workday|ashby/i.test(snapshot) && !/wellfound\.com/i.test(snapshot);
}

export async function driveSubmit(
  plan: SubmitPlan,
  entry: QueueEntry,
  answers: Record<string, string>,
  paths: { stagedShot: string; confirmShot: string },
  env: NodeJS.ProcessEnv,
): Promise<{ result: string; screenshot: string }> {
  await runAxi(["open", plan.url], env);
  let snapshot = await runAxi(["snapshot"], env);
  if (!snapshot.toLowerCase().includes(entry.company.toLowerCase())) {
    fail(`posting no longer shows ${entry.company} - approval was for a different posting`, "NOT_APPROVED");
  }
  if (isExternalAts(snapshot)) return { result: "external-ats", screenshot: paths.stagedShot };
  await runAxi(["click", `@${resolveRef(snapshot, "apply")}`], env);
  snapshot = await runAxi(["snapshot"], env);
  if (isExternalAts(snapshot)) return { result: "external-ats", screenshot: paths.stagedShot };

  const uploadTargets = snapshot.split("\n").filter((line) => /upload|choose file|attach/i.test(line));
  const uploadRefs = [...new Set(uploadTargets.flatMap((line) => [...line.matchAll(/@([A-Za-z0-9_-]+)/g)].map((m) => m[1])))];
  if (uploadRefs.length < 1) fail("no file-upload fields found on the apply form", "BROWSER_STEP");
  await runAxi(["upload", `@${uploadRefs[0]}`, entry.cvPdf as string], env);
  if (uploadRefs.length > 1) await runAxi(["upload", `@${uploadRefs[1]}`, entry.coverPdf as string], env);

  for (const key of entry.requiredAnswers) {
    const ref = resolveRef(snapshot, key);
    await runAxi(["fill", `@${ref}`, answers[key]], env);
  }
  await runAxi(["screenshot", paths.stagedShot], env);

  if (plan.mode === "dry-run") return { result: "staged", screenshot: paths.stagedShot };
  await runAxi(["click", `@${resolveRef(snapshot, "submit")}`], env);
  await runAxi(["screenshot", paths.confirmShot], env);
  return { result: "submitted", screenshot: paths.confirmShot };
}
