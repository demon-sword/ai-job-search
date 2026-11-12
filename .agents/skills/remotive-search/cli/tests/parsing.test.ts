import { describe, test, expect } from "bun:test";
import {
  ageDays,
  cleanHtml,
  normalizeId,
  slugifyCategory,
  toDetail,
  toResult,
  type RemotiveJob,
} from "../src/helpers";
import { filterJobs, matchesCategory, matchesQuery } from "../src/commands/search";

const JOB: RemotiveJob = {
  id: 2091171,
  url: "https://remotive.com/remote-jobs/customer-service/customer-service-representative-2091171",
  title: "Customer Service Representative",
  company_name: "ShyftOff",
  category: "Customer Service",
  tags: ["video", "healthcare"],
  job_type: "contract",
  publication_date: "2026-11-11T09:11:58",
  candidate_required_location: "USA",
  salary: "$14 - $20/hour",
  description: "<p>Hello <strong>world</strong></p><ul><li>one</li><li>two</li></ul>",
};

describe("toResult contract shape", () => {
  test("emits the Step 2 contract fields (id/title/company/location/date/url)", () => {
    const r = toResult(JOB);
    expect(r.id).toBe("2091171");
    expect(r.title).toBe("Customer Service Representative");
    expect(r.company).toBe("ShyftOff");
    expect(r.location).toBe("USA");
    expect(r.date).toBe("2026-11-11T09:11:58");
    expect(r.url).toBe(JOB.url);
  });

  test("missing values are null, never omitted", () => {
    const r = toResult({ ...JOB, company_name: "", candidate_required_location: "", publication_date: null, description: null });
    expect(r.company).toBeNull();
    expect(r.location).toBeNull();
    expect(r.date).toBeNull();
    expect(r.description).toBeNull();
    expect("company" in r && "location" in r && "date" in r && "description" in r).toBe(true);
  });

  test("description is cleaned HTML, not raw markup", () => {
    expect(toResult(JOB).description).toBe("Hello world\n\n- one\n- two");
  });
});

describe("toDetail", () => {
  test("adds enrichment on top of the search result", () => {
    const d = toDetail(JOB);
    expect(d.category).toBe("Customer Service");
    expect(d.job_type).toBe("contract");
    expect(d.salary).toBe("$14 - $20/hour");
    expect(d.tags).toEqual(["video", "healthcare"]);
  });
});

describe("matching", () => {
  test("query tokens AND across title/company/tags/category/description", () => {
    expect(matchesQuery(JOB, "customer shyftoff")).toBe(true);
    expect(matchesQuery(JOB, "customer nonexistent")).toBe(false);
    expect(matchesQuery(JOB, "healthcare")).toBe(true);
  });

  test("category matches label or slug", () => {
    expect(matchesCategory(JOB, [])).toBe(true);
    expect(matchesCategory(JOB, ["customer-service"])).toBe(true);
    expect(matchesCategory(JOB, ["Customer Service"])).toBe(true);
    expect(matchesCategory(JOB, ["software-dev"])).toBe(false);
  });

  test("filterJobs applies jobage", () => {
    const fresh = { ...JOB, publication_date: new Date(Date.now() - 2 * 3_600_000).toISOString() };
    const base = { query: undefined, categories: [] as string[] };
    expect(filterJobs([fresh], { ...base, jobage: 9999 }).length).toBe(1);
    expect(filterJobs([fresh], { ...base, jobage: 1 }).length).toBe(1);
    const old = { ...JOB, publication_date: "2020-01-01T00:00:00" };
    expect(filterJobs([old], { ...base, jobage: 14 }).length).toBe(0);
  });
});

describe("field helpers", () => {
  test("normalizeId accepts bare ids and posting URLs", () => {
    expect(normalizeId("2091171")).toBe("2091171");
    expect(normalizeId(JOB.url)).toBe("2091171");
    expect(normalizeId("nope")).toBeNull();
  });

  test("slugifyCategory turns labels into slugs", () => {
    expect(slugifyCategory("Software Development")).toBe("software-dev");
    expect(slugifyCategory("Customer Service")).toBe("customer-service");
  });

  test("cleanHtml decodes entities and strips tags", () => {
    expect(cleanHtml("<p>a &amp; b</p>")).toBe("a & b");
    expect(cleanHtml(null)).toBeNull();
    expect(cleanHtml("")).toBeNull();
  });

  test("ageDays is null for unparseable dates", () => {
    expect(ageDays(null)).toBeNull();
    expect(ageDays("not-a-date")).toBeNull();
  });
});
