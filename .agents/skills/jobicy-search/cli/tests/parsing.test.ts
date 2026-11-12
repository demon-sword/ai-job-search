import { describe, test, expect } from "bun:test";
import { ageDays, cleanHtml, normalizeId, toDetail, toResult, type JobicyJob } from "../src/helpers";
import { filterJobs, matchesQuery } from "../src/commands/search";

const JOB: JobicyJob = {
  id: 161642,
  url: "https://jobicy.com/jobs/161642-forward-deployed-engineer-10",
  jobSlug: "161642-forward-deployed-engineer-10",
  jobTitle: "Forward Deployed Engineer",
  companyName: "Nearform",
  jobGeo: "Italy",
  jobType: ["Full-Time"],
  jobLevel: "Senior",
  jobIndustry: ["Software Engineering"],
  jobExcerpt: "This is a full-time &amp; permanent opportunity &hellip;",
  jobDescription: "<p>Build <strong>things</strong></p><ul><li>ship</li></ul>",
  pubDate: "2026-11-12T13:29:03+00:00",
  salaryMin: null,
  salaryCurrency: "USD",
  salaryPeriod: null,
};

describe("toResult contract shape", () => {
  test("emits the Step 2 contract fields (id/title/company/location/date/url)", () => {
    const r = toResult(JOB);
    expect(r.id).toBe("161642");
    expect(r.title).toBe("Forward Deployed Engineer");
    expect(r.company).toBe("Nearform");
    expect(r.location).toBe("Italy");
    expect(r.date).toBe("2026-11-12T13:29:03+00:00");
    expect(r.url).toBe(JOB.url);
  });

  test("missing values are null, never omitted", () => {
    const r = toResult({ ...JOB, companyName: "", jobGeo: "", pubDate: null, jobDescription: null });
    expect(r.company).toBeNull();
    expect(r.location).toBeNull();
    expect(r.date).toBeNull();
    expect(r.description).toBeNull();
    expect("company" in r && "location" in r && "date" in r && "description" in r).toBe(true);
  });

  test("description is cleaned HTML, not raw markup", () => {
    expect(toResult(JOB).description).toBe("Build things\n\n- ship");
  });
});

describe("toDetail", () => {
  test("adds status, level, and salary", () => {
    const d = toDetail(JOB, "active");
    expect(d.status).toBe("active");
    expect(d.level).toBe("Senior");
    expect(d.salary).toBeNull();
  });
});

describe("matching", () => {
  test("query tokens AND across title/company/excerpt/description", () => {
    expect(matchesQuery(JOB, "deployed nearform")).toBe(true);
    expect(matchesQuery(JOB, "full-time permanent")).toBe(true);
    expect(matchesQuery(JOB, "deployed acme")).toBe(false);
  });

  test("filterJobs applies jobage", () => {
    const fresh = { ...JOB, pubDate: new Date(Date.now() - 2 * 3_600_000).toISOString() };
    const base = { query: undefined, jobage: 9999 };
    expect(filterJobs([fresh], base).length).toBe(1);
    expect(filterJobs([fresh], { query: undefined, jobage: 1 }).length).toBe(1);
    expect(filterJobs([{ ...JOB, pubDate: "2020-01-01T00:00:00+00:00" }], { query: undefined, jobage: 14 }).length).toBe(0);
  });
});

describe("field helpers", () => {
  test("normalizeId accepts bare ids, slugs, and posting URLs", () => {
    expect(normalizeId("161642")).toBe("161642");
    expect(normalizeId(JOB.jobSlug)).toBe("161642");
    expect(normalizeId(JOB.url)).toBe("161642");
    expect(normalizeId("nope")).toBeNull();
  });

  test("cleanHtml decodes entities and strips tags", () => {
    expect(cleanHtml("<p>a &amp; b &hellip;</p>")).toBe("a & b …");
    expect(cleanHtml(null)).toBeNull();
  });

  test("ageDays is null for unparseable dates", () => {
    expect(ageDays(null)).toBeNull();
    expect(ageDays("not-a-date")).toBeNull();
  });
});
