import { describe, test, expect } from "bun:test";
import { ageDays, toResult, type Jobs24xJob, type JobResult } from "../src/helpers";
import { filterJobs } from "../src/commands/search";

const LOCKED: Jobs24xJob = {
  id: "eb7ed263-0eb0-4c0f-a602-be06f92be",
  title: "Software Engineer, Deployment Compliance",
  description: "Minimum Qualifications. 2+ years ...",
  location: "Not specified",
  remote_status: "On-site",
  employment_type: "Full-time",
  date_posted: "2026-11-11",
  url: "#locked",
  companies: { name: "Jobs" },
  locked: true,
};

describe("toResult contract shape", () => {
  test("emits the Step 2 contract fields (id/title/company/location/date/url)", () => {
    const r = toResult(LOCKED);
    expect(r.id).toBe("eb7ed263-0eb0-4c0f-a602-be06f92be");
    expect(r.title).toBe("Software Engineer, Deployment Compliance");
    expect(r.location).toBe("Not specified · On-site");
    expect(r.date).toBe("2026-11-11");
  });

  test("locked rows null the company and URL, never omit them", () => {
    const r = toResult(LOCKED);
    expect(r.locked).toBe(true);
    expect(r.company).toBeNull();
    expect(r.url).toBeNull();
    expect("company" in r && "url" in r && "locked" in r).toBe(true);
  });

  test("unlocked rows surface company and URL verbatim", () => {
    const r = toResult({ ...LOCKED, locked: false, url: "https://jobs24x.com/jobs/x", companies: { name: "Acme" } });
    expect(r.locked).toBe(false);
    expect(r.company).toBe("Acme");
    expect(r.url).toBe("https://jobs24x.com/jobs/x");
  });
});

describe("filterJobs", () => {
  const rows: JobResult[] = [
    { id: "a", title: "A", company: null, location: null, date: new Date(Date.now() - 86400000).toISOString(), url: null, description: null, locked: true },
    { id: "b", title: "B", company: null, location: null, date: "2020-01-01", url: null, description: null, locked: true },
  ];
  test("jobage filters on the returned page", () => {
    expect(filterJobs(rows, 9999).length).toBe(2);
    expect(filterJobs(rows, 14).map((r) => r.id)).toEqual(["a"]);
  });
});

describe("ageDays", () => {
  test("null for missing/unparseable dates", () => {
    expect(ageDays(null)).toBeNull();
    expect(ageDays("not-a-date")).toBeNull();
  });
});
