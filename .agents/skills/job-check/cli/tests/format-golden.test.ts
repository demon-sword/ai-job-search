import { describe, test, expect } from "bun:test";
import { buildReport, formatTable, type Candidate } from "../src/helpers";

// Pinned fixture: one shortlisted pass, one below-floor, one geo-excluded,
// one stub. The expected table below is the contract — any format change
// must update this golden string deliberately.
const CANDIDATES: Candidate[] = [
  {
    title: "Data Engineer",
    company: "Acme",
    location: "Gurugram, Haryana, India",
    url: "https://example.com/a",
    portal: "example",
    text: "Hiring a data engineer for our Gurugram team. Python, SQL, Spark. CTC Rs 60-70 LPA.",
  },
  {
    title: "Data Engineer",
    company: "Beta",
    location: "Bengaluru, India",
    url: "https://example.com/b",
    portal: "example",
    text: "Data engineer needed. Python and SQL. Salary 30 LPA.",
  },
  {
    title: "Data Engineer",
    company: "Gamma",
    location: "Berlin, Germany",
    url: "https://example.com/c",
    portal: "example",
    text: "Data engineer onsite in Berlin. Python, SQL. Salary 80 LPA equivalent.",
  },
  {
    title: "Data Engineer",
    company: "Delta",
    location: "Remote",
    url: "https://example.com/d",
    portal: "example",
  },
];

const EXPECTED = `Job check — pay bar 55 LPA, min score 0
Shortlist (1)
rank | score/band | title | company | location | pay | india-eligible | url
1 | 55/Moderate Fit | Data Engineer | Acme | Gurugram, Haryana, India | stated-pass | PASS | https://example.com/a
Below floor — pay stated below 55 LPA (1)
rank | score/band | title | company | location | pay | india-eligible | url
2 | 50/Moderate Fit | Data Engineer | Beta | Bengaluru, India | stated-below | FLAG | https://example.com/b
Excluded — not India-eligible (1)
Data Engineer at Gamma — FAIL: based in Germany — https://example.com/c
Stubs — no posting text, never shortlisted (1)
Data Engineer at Delta — detail fetch failed, scored from search snippet only — https://example.com/d`;

describe("ranked-shortlist golden format", () => {
  test("pinned table output", () => {
    const report = buildReport(CANDIDATES, { payBarLpa: 55, minScore: 0, query: "data engineer" });
    expect(formatTable(report)).toBe(EXPECTED);
  });
});
