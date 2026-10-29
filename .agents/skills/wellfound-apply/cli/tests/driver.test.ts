import { describe, expect, test } from "bun:test";
import { buildSubmitPlan, isExternalAts, resolveRef } from "../src/driver";
import type { QueueEntry } from "../src/pilot";

const ENTRY: QueueEntry = {
  job: "test-job",
  company: "TestCo",
  role: "Tester",
  url: "https://wellfound.com/jobs/1-test",
  status: "approved",
  cvPdf: "cv.pdf",
  coverPdf: "cover.pdf",
  requiredAnswers: ["notice_period_days"],
  approvedBy: "Captain",
  approvedAt: "2026-10-29T00:00:00Z",
  notes: "",
};

describe("resolveRef", () => {
  test("resolves a unique accessible-name match", () => {
    const snapshot = ["button 'Apply now' @e12", "link 'About TestCo' @e40"].join("\n");
    expect(resolveRef(snapshot, "apply")).toBe("e12");
  });
});

describe("isExternalAts", () => {
  test("flags an off-Wellfound ATS page", () => {
    expect(isExternalAts("job board powered by greenhouse.io")).toBe(true);
  });

  test("keeps a native Wellfound form", () => {
    expect(isExternalAts("Apply on wellfound.com - TestCo")).toBe(false);
  });
});

describe("buildSubmitPlan", () => {
  test("dry-run has no submit click", () => {
    const plan = buildSubmitPlan(ENTRY, "dry-run", "shot.png");
    expect(plan.steps.some((s) => s.action === "click-submit")).toBe(false);
    expect(plan.steps.at(-1)?.action).toBe("screenshot");
  });

  test("live appends submit click plus confirmation screenshot", () => {
    const plan = buildSubmitPlan(ENTRY, "live", "shot.png");
    const actions = plan.steps.map((s) => s.action);
    expect(actions.at(-2)).toBe("click-submit");
    expect(actions.at(-1)).toBe("screenshot-confirmation");
  });
});
