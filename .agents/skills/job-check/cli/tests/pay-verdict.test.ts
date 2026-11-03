import { describe, test, expect } from "bun:test";
import { extractPayFigures, payVerdict } from "../src/helpers";

describe("extractPayFigures", () => {
  test("LPA range keeps min and max with evidence", () => {
    const figs = extractPayFigures("CTC: Rs 60-70 LPA depending on experience.");
    expect(figs).not.toBeNull();
    expect(figs!.minLpa).toBe(60);
    expect(figs!.maxLpa).toBe(70);
    expect(figs!.evidence.length).toBeGreaterThan(0);
  });

  test("single LPA figure", () => {
    const figs = extractPayFigures("We offer 45 LPA plus ESOPs.");
    expect(figs).not.toBeNull();
    expect(figs!.minLpa).toBe(45);
    expect(figs!.maxLpa).toBe(45);
  });

  test("annual INR figure converts to LPA", () => {
    const figs = extractPayFigures("Annual CTC INR 55,00,000.");
    expect(figs).not.toBeNull();
    expect(figs!.minLpa).toBeCloseTo(55, 5);
  });

  test("monthly INR figure annualizes to LPA", () => {
    const figs = extractPayFigures("Take-home Rs 3,00,000 per month.");
    expect(figs).not.toBeNull();
    expect(figs!.minLpa).toBeCloseTo(36, 5);
  });

  test("annual USD figure converts with the documented rate", () => {
    const figs = extractPayFigures("Salary: $120,000 per year.");
    expect(figs).not.toBeNull();
    expect(figs!.minLpa).toBeGreaterThan(55);
  });

  test("returns null when no figure is stated", () => {
    expect(extractPayFigures("Competitive salary. Great culture.")).toBeNull();
    expect(extractPayFigures("Pay: TBD on experience.")).toBeNull();
  });

  test("does not mistake headcount or years for pay", () => {
    expect(extractPayFigures("We are a team of 120 hiring 5 engineers.")).toBeNull();
  });
});

describe("payVerdict against the bar", () => {
  test("range maximum at/above the bar is stated-pass", () => {
    expect(payVerdict({ minLpa: 50, maxLpa: 60, evidence: [] }, 55)).toBe("stated-pass");
  });

  test("entire band below the bar is stated-below", () => {
    expect(payVerdict({ minLpa: 30, maxLpa: 40, evidence: [] }, 55)).toBe("stated-below");
  });

  test("no stated figure needs verification", () => {
    expect(payVerdict(null, 55)).toBe("undisclosed-needs-verification");
  });

  test("the bar is configurable, not hardcoded to 55", () => {
    expect(payVerdict({ minLpa: 30, maxLpa: 40, evidence: [] }, 30)).toBe("stated-pass");
    expect(payVerdict({ minLpa: 30, maxLpa: 40, evidence: [] }, 41)).toBe("stated-below");
  });
});
