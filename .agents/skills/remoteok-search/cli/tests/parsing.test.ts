import { describe, test, expect } from "bun:test";
import { ageDays, cleanHtml, formatSalary, normalizeId, toDetail, toResult, type RemoteOKJob } from "../src/helpers";
import { filterJobs, matchesQuery } from "../src/commands/search";

const JOB: RemoteOKJob = {
  id: "1137714",
  slug: "remote-data-engineer-addepto-1137714",
  date: "2026-11-10T16:00:04+00:00",
  company: "Addepto",
  position: "Data Engineer",
  tags: ["python", "cloud"],
  description: "<p>Build <strong>pipelines</strong></p><ul><li>etl</li></ul>",
  location: "",
  url: "https://remoteOK.com/remote-jobs/remote-data-engineer-addepto-1137714",
  apply_url: "https://remoteok.com/apply/1137714",
  salary_min: 0,
  salary_max: 0,
};

describe("toResult contract shape", () => {
  test("emits the Step 2 contract fields (id/title/company/location/date/url)", () => {
    const r = toResult(JOB);
    expect(r.id).toBe("1137714");
    expect(r.title).toBe("Data Engineer");
    expect(r.company).toBe("Addepto");
    expect(r.location).toBeNull();
    expect(r.date).toBe("2026-11-10T16:00:04+00:00");
    expect(r.url).toBe(JOB.url);
  });

  test("empty location and undisclosed salary surface as null", () => {
    const d = toDetail(JOB);
    expect(d.location).toBeNull();
    expect(d.salary).toBeNull();
  });

  test("description is cleaned HTML, not raw markup", () => {
    expect(toResult(JOB).description).toBe("Build pipelines\n\n- etl");
  });
});

describe("matching", () => {
  test("query tokens AND across position/company/tags/location/description", () => {
    expect(matchesQuery(JOB, "data addepto")).toBe(true);
    expect(matchesQuery(JOB, "python")).toBe(true);
    expect(matchesQuery(JOB, "data golang")).toBe(false);
  });

  test("filterJobs applies jobage", () => {
    const fresh = { ...JOB, date: new Date(Date.now() - 2 * 3_600_000).toISOString() };
    const base = { query: undefined, jobage: 9999 };
    expect(filterJobs([fresh], base).length).toBe(1);
    expect(filterJobs([fresh], { query: undefined, jobage: 1 }).length).toBe(1);
    const old = { ...JOB, date: "2020-01-01T00:00:00+00:00" };
    expect(filterJobs([old], { query: undefined, jobage: 14 }).length).toBe(0);
  });
});

describe("field helpers", () => {
  test("normalizeId accepts bare ids, slugs, and posting URLs", () => {
    expect(normalizeId("1137714")).toBe("1137714");
    expect(normalizeId(JOB.slug)).toBe("1137714");
    expect(normalizeId(JOB.url)).toBe("1137714");
    expect(normalizeId("nope")).toBeNull();
  });

  test("formatSalary treats 0 as undisclosed", () => {
    expect(formatSalary(0, 0)).toBeNull();
    expect(formatSalary(90000, 120000)).toBe("$90000 - $120000");
    expect(formatSalary(90000, 90000)).toBe("$90000");
  });

  test("cleanHtml decodes entities and strips tags", () => {
    expect(cleanHtml("<p>a &amp; b</p>")).toBe("a & b");
    expect(cleanHtml(null)).toBeNull();
  });

  test("ageDays is null for unparseable dates", () => {
    expect(ageDays(null)).toBeNull();
    expect(ageDays("not-a-date")).toBeNull();
  });
});
