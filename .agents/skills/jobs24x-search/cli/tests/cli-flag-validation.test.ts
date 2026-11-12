import { describe, test, expect } from "bun:test";
import { runCLI } from "./helpers";

// These assert on validation error codes that are emitted BEFORE any network
// call, so the suite is network-free.

function parsedStderr(stderr: string): { error?: string; code?: string } {
  try {
    return JSON.parse(stderr);
  } catch {
    return {};
  }
}

describe("jobs24x CLI flag validation", () => {
  describe("numeric flag validation", () => {
    for (const name of ["jobage", "page", "limit", "max-pages"]) {
      const args = name === "max-pages" ? ["detail", "eb7ed263-0eb0-4c0f-a602-be06f92be"] : ["search"];
      test(`--${name} abc exits 1 with BAD_ARG`, async () => {
        const r = await runCLI([...args, `--${name}`, "abc"]);
        expect(r.exitCode).toBe(1);
        expect(parsedStderr(r.stderr).code).toBe("BAD_ARG");
      });

      test(`--${name} 0 exits 1 with BAD_ARG`, async () => {
        const r = await runCLI([...args, `--${name}`, "0"]);
        expect(r.exitCode).toBe(1);
        expect(parsedStderr(r.stderr).code).toBe("BAD_ARG");
      });
    }

    test("valid integers produce no BAD_ARG", async () => {
      const r = await runCLI(["search", "--page", "1", "--limit", "2"]);
      if (r.exitCode !== 0) expect(parsedStderr(r.stderr).code).not.toBe("BAD_ARG");
    });
  });

  describe("format validation", () => {
    test("--format bogus exits 1 with BAD_ARG", async () => {
      const r = await runCLI(["search", "--format", "yaml"]);
      expect(r.exitCode).toBe(1);
      expect(parsedStderr(r.stderr).code).toBe("BAD_ARG");
    });
  });

  describe("detail argument validation", () => {
    test("detail without a UUID exits 1 with NO_ID", async () => {
      const r = await runCLI(["detail"]);
      expect(r.exitCode).toBe(1);
      expect(parsedStderr(r.stderr).code).toBe("NO_ID");
    });

    test("detail with a non-UUID exits 1 with BAD_ID", async () => {
      const r = await runCLI(["detail", "nope"]);
      expect(r.exitCode).toBe(1);
      expect(parsedStderr(r.stderr).code).toBe("BAD_ID");
    });
  });

  describe("command dispatch", () => {
    test("unknown command exits 1 with BAD_CMD", async () => {
      const r = await runCLI(["frobnicate"]);
      expect(r.exitCode).toBe(1);
      expect(parsedStderr(r.stderr).code).toBe("BAD_CMD");
    });
  });
});

describe("unknown flag rejection", () => {
  // add-portal.md's contract: a bogus flag exits 1 with a JSON error on
  // stderr, never silently discarded.
  test("a bogus --flag exits 1 with UNKNOWN_FLAG", async () => {
    const r = await runCLI(["search", "--bogus", "x"]);
    expect(r.exitCode).toBe(1);
    expect(parsedStderr(r.stderr).code).toBe("UNKNOWN_FLAG");
  });
});
