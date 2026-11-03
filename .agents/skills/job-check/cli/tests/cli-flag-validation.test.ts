import { describe, test, expect } from "bun:test";
import { runCLI, parseJSON } from "./helpers";
import { join } from "path";
import { tmpdir } from "os";

function errParts(stderr: string): { error?: string; code?: string } {
  try {
    return JSON.parse(stderr);
  } catch {
    return {};
  }
}

describe("job-check CLI flag validation", () => {
  test("unknown flag exits 1 with BAD_ARG", async () => {
    const result = await runCLI(["check", "--bogus-flag"]);
    expect(result.exitCode).toBe(1);
    expect(errParts(result.stderr).code).toBe("BAD_ARG");
  });

  test("non-numeric --pay-bar-lpa exits 1 with BAD_ARG", async () => {
    const result = await runCLI(["check", "--pay-bar-lpa", "lots"]);
    expect(result.exitCode).toBe(1);
    expect(errParts(result.stderr).code).toBe("BAD_ARG");
  });

  test("--help exits 0 without network", async () => {
    const result = await runCLI(["--help"]);
    expect(result.exitCode).toBe(0);
    expect(result.stdout).toMatch(/check/);
  });

  test("missing --input file exits 1 without network", async () => {
    const result = await runCLI(["check", "--input", "/nonexistent/jobs.json"]);
    expect(result.exitCode).toBe(1);
    expect(result.stderr).toBeTruthy();
  });

  test("offline --input run emits all report sections as JSON", async () => {
    const fixture = join(tmpdir(), `job-check-fixture-${Date.now()}.json`);
    await Bun.write(
      fixture,
      JSON.stringify([
        {
          title: "Data Engineer",
          company: "Acme",
          location: "Gurugram, India",
          url: "https://example.com/a",
          text: "Data engineer in Gurugram. Python, SQL. CTC 60 LPA.",
        },
      ])
    );
    const result = await runCLI([
      "check",
      "--input",
      fixture,
      "--format",
      "json",
      "--min-score",
      "0",
      "--query",
      "data engineer",
    ]);
    const report = parseJSON<{
      shortlist: unknown[];
      below_floor: unknown[];
      excluded: unknown[];
      stubs: unknown[];
      portal_errors: unknown[];
    }>(result);
    expect(report.shortlist).toHaveLength(1);
    for (const key of ["below_floor", "excluded", "stubs", "portal_errors"] as const) {
      expect(report[key]).toBeDefined();
    }
  });
});
