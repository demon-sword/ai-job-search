import { describe, test, expect } from "bun:test";
import { buildReport, type Candidate } from "../src/helpers";

describe("stubs never shortlist", () => {
  test("a stub with perfect query overlap still lands in stubs", () => {
    const candidates: Candidate[] = [
      {
        title: "Data Engineer",
        company: "StubCo",
        location: "Gurugram, India",
        url: "https://example.com/stub",
        portal: "example",
        snippet: "Data engineer data engineer data engineer",
      },
      {
        title: "Accountant",
        company: "RealCo",
        location: "Gurugram, India",
        url: "https://example.com/real",
        portal: "example",
        text: "Hiring an accountant for invoicing. CTC 60 LPA. Gurugram office.",
      },
    ];
    const report = buildReport(candidates, { payBarLpa: 55, minScore: 0, query: "data engineer" });
    expect(report.stubs.map((s) => s.url)).toContain("https://example.com/stub");
    expect(report.shortlist.map((s) => s.url)).not.toContain("https://example.com/stub");
    for (const row of report.shortlist) {
      expect(row.provenance).toBe("full_text");
    }
  });

  test("stub rows carry their provenance and text source", () => {
    const report = buildReport(
      [
        {
          title: "X",
          company: "Y",
          location: "Remote",
          url: "https://example.com/x",
          portal: "example",
        },
      ],
      { payBarLpa: 55, minScore: 0, query: "x" }
    );
    expect(report.shortlist).toHaveLength(0);
    expect(report.stubs).toHaveLength(1);
    expect(report.stubs[0].provenance).toBe("stub");
  });
});
