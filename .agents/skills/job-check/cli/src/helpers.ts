// Pure job-triage logic for the job-check skill: pay extraction, the pay-bar
// comparison, India-eligibility, heuristic scoring, and the ranked-shortlist
// report. No I/O here — orchestration (portal discovery, fetching, caching)
// lives in commands/check.ts, so every function below is unit-testable.

export const DEFAULT_PAY_BAR_LPA = 55;
export const DEFAULT_MIN_SCORE = 45;

// Documented conversion constant for foreign-currency figures. A single
// pinned rate keeps verdicts reproducible; re-run with fresh text when the
// rate moves materially.
export const USD_TO_INR = 83;

export interface PayFigures {
  minLpa: number;
  maxLpa: number;
  evidence: string[];
}

export type PayVerdict =
  | "stated-pass"
  | "stated-below"
  | "undisclosed-needs-verification";

export interface GeoResult {
  verdict: "PASS" | "FAIL" | "FLAG";
  reason: string;
}

export interface Candidate {
  title: string;
  company: string;
  location: string;
  url: string;
  portal?: string;
  date?: string;
  text?: string;
  snippet?: string;
  stubReason?: string;
}

export type Provenance = "full_text" | "stub";

export interface RankedRow {
  rank: number;
  title: string;
  company: string;
  location: string;
  score: number;
  band: string;
  pay_verdict: PayVerdict;
  pay_evidence: string[];
  geo_verdict: "PASS" | "FAIL" | "FLAG";
  geo_reason: string;
  url: string;
  provenance: Provenance;
  text_source: string;
}

export interface StubRow {
  title: string;
  company: string;
  location: string;
  url: string;
  portal?: string;
  provenance: Provenance;
  text_source: string;
  reason: string;
}

export interface ExcludedRow {
  title: string;
  company: string;
  location: string;
  url: string;
  geo_verdict: "FAIL";
  geo_reason: string;
  provenance: Provenance;
}

export interface CheckReport {
  shortlist: RankedRow[];
  below_floor: RankedRow[];
  excluded: ExcludedRow[];
  stubs: StubRow[];
  portal_errors: { portal: string; error: string }[];
  meta: {
    pay_bar_lpa: number;
    min_score: number;
    query: string;
    candidates: number;
    full_text: number;
    stubs: number;
  };
}

const LAKH = 100_000;

function cleanAmount(raw: string): number {
  return Number(raw.replace(/,/g, ""));
}

export function extractPayFigures(text: string): PayFigures | null {
  const evidence: string[] = [];
  const figures: number[] = [];
  let rest = text;

  // LPA ranges first ("60-70 LPA"), removed before the single-figure pass so
  // the range maximum is not double-counted as its own single figure.
  rest = rest.replace(
    /(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)\s*(?:lpa)\b/gi,
    (_m, a: string, b: string) => {
      figures.push(Number(a), Number(b));
      evidence.push(`${a}-${b} LPA`);
      return " ";
    }
  );
  rest.replace(
    /(\d+(?:\.\d+)?)\s*(?:lpa|lakhs?\s+per\s+annum|lacs?\s+per\s+annum)\b/gi,
    (m, a: string) => {
      figures.push(Number(a));
      evidence.push(m.trim());
      return m;
    }
  );

  // Rupee amounts, scanned on the range-stripped copy so an LPA range's
  // first number is not re-read as a rupee figure. Annual cues annualize
  // directly; monthly cues annualize x12. A bare amount with no cue is
  // skipped rather than guessed.
  for (const m of rest.matchAll(/(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)/gi)) {
    const amount = cleanAmount(m[1]);
    if (!Number.isFinite(amount) || amount <= 0) continue;
    const window = rest.slice(Math.max(0, (m.index ?? 0) - 30), (m.index ?? 0) + m[0].length + 30);
    if (/(per\s+month|monthly|\/month|p\.m\.)/i.test(window)) {
      figures.push((amount * 12) / LAKH);
      evidence.push(`${m[0].trim()} (monthly, annualized)`);
    } else if (/(per\s+annum|p\.a\.|annual|yearly|per\s+year|ctc)/i.test(window)) {
      figures.push(amount / LAKH);
      evidence.push(m[0].trim());
    }
  }

  // USD annual figures, converted at the documented pinned rate.
  for (const m of text.matchAll(/(?:\$\s*|usd\s*)([\d,]+(?:\.\d+)?)\s*(k)?/gi)) {
    let amount = cleanAmount(m[1]);
    if (!Number.isFinite(amount) || amount <= 0) continue;
    if (m[2]) amount *= 1000;
    if (amount < 1000) continue; // not a plausible annual salary figure
    figures.push((amount * USD_TO_INR) / LAKH);
    evidence.push(`${m[0].trim()} (USD, converted)`);
  }

  if (figures.length === 0) return null;
  return {
    minLpa: Math.min(...figures),
    maxLpa: Math.max(...figures),
    evidence: evidence.slice(0, 3),
  };
}

export function payVerdict(figs: PayFigures | null, barLpa: number): PayVerdict {
  if (!figs) return "undisclosed-needs-verification";
  // Honest floor: only an entirely-below-bar band is stated-below. A range
  // that straddles the bar still clears it at the top end.
  return figs.maxLpa >= barLpa ? "stated-pass" : "stated-below";
}

const NCR_TOKENS = [
  "new delhi",
  "delhi",
  "gurgaon",
  "gurugram",
  "noida",
  "greater noida",
  "faridabad",
  "ghaziabad",
];

const INDIA_CITIES = [
  "bengaluru", "bangalore", "mumbai", "hyderabad", "chennai", "pune",
  "kolkata", "ahmedabad", "jaipur", "kochi", "thiruvananthapuram",
  "lucknow", "chandigarh", "indore", "nagpur", "surat", "coimbatore",
  "bhubaneswar", "mysuru", "mysore", "vijayawada", "vadodara", "trivandrum",
];

const NON_INDIA_PLACES = [
  "united states", "usa", "u.s.a", "america", "new york", "san francisco",
  "austin", "seattle", "boston", "chicago", "germany", "berlin", "munich",
  "united kingdom", "u.k", "london", "canada", "toronto", "vancouver",
  "australia", "sydney", "melbourne", "singapore", "uae", "dubai",
  "france", "paris", "netherlands", "amsterdam", "ireland", "dublin",
  "spain", "madrid", "italy", "milan", "poland", "warsaw", "sweden",
  "stockholm", "norway", "oslo", "denmark", "copenhagen", "aarhus",
  "odense", "finland", "helsinki", "switzerland", "zurich", "japan",
  "tokyo", "china", "beijing", "brazil", "mexico", "israel", "tel aviv",
  "europe", "european union",
];

function wordHit(haystack: string, needle: string): boolean {
  return new RegExp(`\\b${needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i").test(haystack);
}

export function geoVerdict(location: string, text: string): GeoResult {
  const combined = `${location} ${text}`;
  const loc = location.toLowerCase();

  for (const city of NCR_TOKENS) {
    if (wordHit(combined, city)) {
      return { verdict: "PASS", reason: `in NCR (${city})` };
    }
  }

  if (/(authoriz\w*\s+to\s+work|sponsorship|no\s+sponsor|must\s+(reside|be\s+based|live)\s+in|only\s+\w*\s*(residents|citizens)|citizenship|work\s+permit|eligible\s+to\s+work\s+in)/i.test(combined)) {
    if (!/\bindia\b/i.test(combined)) {
      return {
        verdict: "FAIL",
        reason: "requires non-India work authorization or offers no sponsorship",
      };
    }
  }

  for (const place of NON_INDIA_PLACES) {
    if (wordHit(loc, place)) {
      const country = place
        .split(" ")
        .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
        .join(" ");
      return { verdict: "FAIL", reason: `based in ${country}` };
    }
  }

  const remote = /\bremote\b|work\s+from\s+home|\bwfh\b|distributed/i.test(combined);
  const india = /\bindia\b/i.test(combined);
  if (remote && india) {
    return { verdict: "PASS", reason: "India-remote" };
  }
  if (/(anywhere|worldwide|across the globe|all countries|no matter where|globally?\s+(remote|distributed|hiring))/i.test(combined)) {
    return { verdict: "PASS", reason: "worldwide-remote" };
  }

  for (const city of INDIA_CITIES) {
    if (wordHit(combined, city)) {
      return { verdict: "FLAG", reason: "in India but outside NCR — relocation call is yours" };
    }
  }
  if (india) {
    return { verdict: "FLAG", reason: "India-listed but no city — verify before applying" };
  }
  if (remote) {
    return { verdict: "FLAG", reason: "remote with no country signal — verify India eligibility" };
  }
  return { verdict: "FLAG", reason: "no location signal — verify before applying" };
}

export function scoreBand(score: number): string {
  if (score >= 75) return "Strong Fit";
  if (score >= 60) return "Good Fit";
  if (score >= 45) return "Moderate Fit";
  if (score >= 30) return "Weak Fit";
  return "Poor Fit";
}

const SKILL_KEYWORDS = [
  "python", "sql", "spark", "aws", "gcp", "azure", "kafka", "airflow",
  "dbt", "kubernetes", "docker", "terraform", "java", "go", "typescript",
  "react", "machine learning", "pandas", "snowflake", "bigquery",
  "redshift", "postgres", "scala", "flink", "hadoop", "etl", "llm",
  "pytorch", "tensorflow",
];

export function queryTerms(query: string): string[] {
  return [...new Set((query.toLowerCase().match(/[a-z0-9]+/g) ?? []))];
}

// Heuristic triage only: query-term coverage of the title and text plus a
// small bonus for a substantive skill list. Stated plainly in the SKILL.md
// and the report — /apply's full evaluation stays authoritative.
export function heuristicScore(title: string, text: string, terms: string[]): number {
  if (terms.length === 0) return 0;
  const titleLow = title.toLowerCase();
  const textLow = text.toLowerCase();
  const titleHits = terms.filter((t) => wordHit(titleLow, t)).length;
  const textHits = terms.filter((t) => wordHit(textLow, t)).length;
  let score = (40 * titleHits) / terms.length + (10 * textHits) / terms.length;
  const skillHits = SKILL_KEYWORDS.filter(
    (k) => !terms.includes(k) && wordHit(textLow, k)
  ).length;
  if (skillHits >= 3) score += 5;
  return Math.round(score);
}

export const DEFAULT_STUB_REASON =
  "detail fetch failed, scored from search snippet only";

export function normalizeUrl(url: string): string {
  return url
    .trim()
    .replace(/#.*$/, "")
    .replace(/\?.*$/, "")
    .replace(/\/$/, "")
    .toLowerCase();
}

export interface BuildOpts {
  payBarLpa: number;
  minScore: number;
  query: string;
}

export function buildReport(candidates: Candidate[], opts: BuildOpts): CheckReport {
  const terms = queryTerms(opts.query);
  const report: CheckReport = {
    shortlist: [],
    below_floor: [],
    excluded: [],
    stubs: [],
    portal_errors: [],
    meta: {
      pay_bar_lpa: opts.payBarLpa,
      min_score: opts.minScore,
      query: opts.query,
      candidates: candidates.length,
      full_text: 0,
      stubs: 0,
    },
  };

  const eligible: RankedRow[] = [];
  for (const c of candidates) {
    const portal = c.portal ?? "input";
    const text = (c.text ?? "").trim();
    if (text.length < 20) {
      // A stub is never shortlisted, whatever the title match.
      report.stubs.push({
        title: c.title,
        company: c.company,
        location: c.location,
        url: c.url,
        portal,
        provenance: "stub",
        text_source: `${portal}:search-snippet`,
        reason: c.stubReason ?? DEFAULT_STUB_REASON,
      });
      continue;
    }
    const figs = extractPayFigures(text);
    const geo = geoVerdict(c.location, text);
    const score = heuristicScore(c.title, text, terms);
    if (geo.verdict === "FAIL") {
      report.excluded.push({
        title: c.title,
        company: c.company,
        location: c.location,
        url: c.url,
        geo_verdict: "FAIL",
        geo_reason: geo.reason,
        provenance: "full_text",
      });
      continue;
    }
    eligible.push({
      rank: 0,
      title: c.title,
      company: c.company,
      location: c.location,
      score,
      band: scoreBand(score),
      pay_verdict: payVerdict(figs, opts.payBarLpa),
      pay_evidence: figs?.evidence ?? [],
      geo_verdict: geo.verdict,
      geo_reason: geo.reason,
      url: c.url,
      provenance: "full_text",
      text_source: `${portal}:detail`,
    });
  }

  eligible.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title));
  eligible.forEach((row, i) => {
    row.rank = i + 1;
  });
  for (const row of eligible) {
    // Honestly below floor: an entirely-below-bar band is listed separately,
    // never hidden and never promoted. Sub-threshold scores join it — they
    // are below the shortlist floor on fit rather than pay.
    if (row.pay_verdict === "stated-below" || row.score < opts.minScore) {
      report.below_floor.push(row);
    } else {
      report.shortlist.push(row);
    }
  }

  report.meta.full_text = eligible.length + report.excluded.length;
  report.meta.stubs = report.stubs.length;
  return report;
}

const ROW_HEADER =
  "rank | score/band | title | company | location | pay | india-eligible | url";

function tableRow(r: RankedRow): string {
  return `${r.rank} | ${r.score}/${r.band} | ${r.title} | ${r.company} | ${r.location} | ${r.pay_verdict} | ${r.geo_verdict} | ${r.url}`;
}

export function formatTable(report: CheckReport): string {
  const bar = report.meta.pay_bar_lpa;
  const min = report.meta.min_score;
  const lines = [
    `Job check — pay bar ${bar} LPA, min score ${min}`,
    `Shortlist (${report.shortlist.length})`,
  ];
  if (report.shortlist.length === 0) {
    lines.push("(none)");
  } else {
    lines.push(ROW_HEADER);
    for (const r of report.shortlist) lines.push(tableRow(r));
  }
  if (report.below_floor.length > 0) {
    lines.push(`Below floor — pay stated below ${bar} LPA (${report.below_floor.length})`);
    lines.push(ROW_HEADER);
    for (const r of report.below_floor) lines.push(tableRow(r));
  }
  if (report.excluded.length > 0) {
    lines.push(`Excluded — not India-eligible (${report.excluded.length})`);
    for (const e of report.excluded) {
      lines.push(`${e.title} at ${e.company} — FAIL: ${e.geo_reason} — ${e.url}`);
    }
  }
  if (report.stubs.length > 0) {
    lines.push(`Stubs — no posting text, never shortlisted (${report.stubs.length})`);
    for (const s of report.stubs) {
      lines.push(`${s.title} at ${s.company} — ${s.reason} — ${s.url}`);
    }
  }
  return lines.join("\n");
}

export function formatPlain(report: CheckReport): string {
  const lines: string[] = [];
  for (const r of [...report.shortlist, ...report.below_floor]) {
    lines.push(
      `${r.rank}. ${r.title} at ${r.company} [${r.band} ${r.score}] pay=${r.pay_verdict} geo=${r.geo_verdict} ${r.url}`
    );
  }
  for (const e of report.excluded) {
    lines.push(`x ${e.title} at ${e.company} geo=FAIL (${e.geo_reason}) ${e.url}`);
  }
  for (const s of report.stubs) {
    lines.push(`? ${s.title} at ${s.company} stub (${s.reason}) ${s.url}`);
  }
  return lines.join("\n");
}
