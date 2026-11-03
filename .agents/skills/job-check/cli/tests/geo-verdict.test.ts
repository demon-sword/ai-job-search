import { describe, test, expect } from "bun:test";
import { geoVerdict } from "../src/helpers";

describe("geoVerdict India-eligibility", () => {
  test("NCR city passes", () => {
    for (const loc of ["Gurugram, Haryana, India", "Noida, India", "New Delhi, India"]) {
      const r = geoVerdict(loc, "Work from our Gurugram office.");
      expect(r.verdict).toBe("PASS");
    }
  });

  test("India-remote passes", () => {
    const r = geoVerdict("Remote", "This is a remote role open across India. Work from home.");
    expect(r.verdict).toBe("PASS");
  });

  test("worldwide-remote passes", () => {
    const r = geoVerdict("Remote", "We hire anywhere in the world; fully distributed team.");
    expect(r.verdict).toBe("PASS");
  });

  test("country-listed outside India fails with a reason", () => {
    const r = geoVerdict("Berlin, Germany", "Onsite in Berlin.");
    expect(r.verdict).toBe("FAIL");
    expect(r.reason.length).toBeGreaterThan(0);
  });

  test("work-auth restriction fails with a reason", () => {
    const r = geoVerdict("Remote", "Must be authorized to work in the United States. No sponsorship.");
    expect(r.verdict).toBe("FAIL");
    expect(r.reason).toMatch(/authoriz|sponsor/i);
  });

  test("bare remote with no country signal flags as unclear", () => {
    const r = geoVerdict("Remote", "A remote role on a distributed team.");
    expect(r.verdict).toBe("FLAG");
    expect(r.reason.length).toBeGreaterThan(0);
  });

  test("empty location and text flags as unclear", () => {
    const r = geoVerdict("", "");
    expect(r.verdict).toBe("FLAG");
  });
});
