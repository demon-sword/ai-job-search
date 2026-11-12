import { describe, test, expect } from "bun:test";
import { ageDays, cleanHtml, extractId, normalizeId, splitTags, toDetail, toResult, type WorkingNomadsJob } from "../src/helpers";
import { filterJobs, matchesCategory, matchesQuery } from "../src/commands/search";

const JOB: WorkingNomadsJob = {
  url: "https://www.workingnomads.com/job/go/2016531/",
  title: "Senior React Full-stack Developer",
  description: "<p>Ship <strong>things</strong></p><ul><li>react</li></ul>",
  company_name: "Lemon.io",
  category_name: "Development",
  location: "Europe, LATAM, APAC, the U.S., Canada",
  tags: "react,python,nodejs,ruby,english",
  pub_date: "2026-11-12T06:47:52-05:00",
};

describe("toResult contract shape", () => {
  test("emits the Step 2 contract fields (id/title/company/location/date/url)", () => {
    const r = toResult(JOB);
    expect(r.id).toBe("2016531");
    expect(r.title).toBe("Senior React Full-stack Developer");
    expect(r.company).toBe("Lemon.io");
    expect(r.location).toBe("Europe, LATAM, APAC, the U.S., Canada");
    expect(r.date).toBe("2026-11-12T06:47:52-05:00");
    expect(r.url).toBe(JOB.url);
  });

  test("missing values are null, never omitted", () => {
    const r = toResult({ ...JOB, company_name: "", location: "", pub_date: null, description: null });
    expect(r.company).toBeNull();
    expect(r.location).toBeNull();
    expect(r.date).toBeNull();
    expect(r.description).toBeNull();
    expect("company" in r && "location" in r && "date" in r && "description" in r).toBe(true);
  });

  test("description is cleaned HTML, not raw markup", () => {
    expect(toResult(JOB).description).toBe("Ship things\n\n- react");
  });
});

describe("toDetail", () => {
  test("adds category and split tags", () => {
    const d = toDetail(JOB);
    expect(d.category).toBe("Development");
    expect(d.tags).toEqual(["react", "python", "nodejs", "ruby", "english"]);
  });
});

describe("matching", () => {
  test("query tokens AND across title/company/category/location/tags/description", () => {
    expect(matchesQuery(JOB, "react lemon")).toBe(true);
    expect(matchesQuery(JOB, "python")).toBe(true);
    expect(matchesQuery(JOB, "react acme")).toBe(false);
  });

  test("category matches case-insensitively", () => {
    expect(matchesCategory(JOB, [])).toBe(true);
    expect(matchesCategory(JOB, ["development"])).toBe(true);
    expect(matchesCategory(JOB, ["Marketing"])).toBe(false);
  });

  test("filterJobs applies jobage", () => {
    const fresh = { ...JOB, pub_date: new Date(Date.now() - 2 * 3_600_000).toISOString() };
    const base = { query: undefined, categories: [] as string[], jobage: 9999 };
    expect(filterJobs([fresh], base).length).toBe(1);
    expect(filterJobs([fresh], { ...base, jobage: 1 }).length).toBe(1);
    expect(filterJobs([{ ...JOB, pub_date: "2020-01-01T00:00:00-05:00" }], { ...base, jobage: 14 }).length).toBe(0);
  });
});

describe("field helpers", () => {
  test("extractId/normalizeId handle bare ids and go URLs", () => {
    expect(extractId(JOB.url)).toBe("2016531");
    expect(normalizeId("2016531")).toBe("2016531");
    expect(normalizeId(JOB.url)).toBe("2016531");
    expect(normalizeId("nope")).toBeNull();
  });

  test("splitTags splits CSV and drops empties", () => {
    expect(splitTags("a, b,,c ")).toEqual(["a", "b", "c"]);
    expect(splitTags("")).toEqual([]);
    expect(splitTags(null)).toEqual([]);
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
