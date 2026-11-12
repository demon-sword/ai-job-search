import {
  API_BASE,
  ageDays,
  apiGet,
  toResult,
  writeError,
  type JobicyJob,
  type JobResult,
  type SearchResponse,
} from "../helpers.js";

export interface SearchOpts {
  query?: string;
  tag?: string;
  geo?: string;
  industry?: string;
  jobage: number;
  page: number;
  limit: number;
  format: "json" | "table" | "plain";
  descriptionFormat: "markdown" | "text";
  includeDescription: boolean;
}

/** True when the job matches every query token (AND) across searchable text. */
export function matchesQuery(j: JobicyJob, query: string): boolean {
  const hay = [j.jobTitle, j.companyName, j.jobExcerpt ?? "", j.jobDescription ?? ""]
    .join("\n")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((tok) => hay.includes(tok));
}

function buildUrl(opts: SearchOpts, cursor?: string): string {
  const params = new URLSearchParams();
  params.set("count", String(Math.min(opts.limit, 100)));
  if (opts.tag) params.set("tag", opts.tag);
  if (opts.geo) params.set("geo", opts.geo);
  if (opts.industry) params.set("industry", opts.industry);
  if (cursor) params.set("cursor", cursor);
  return `${API_BASE}?${params.toString()}`;
}

/** Fetch one page of the feed, walking cursors for --page > 1. */
export async function fetchPage(opts: SearchOpts): Promise<{ jobs: JobicyJob[]; total: number }> {
  let cursor: string | undefined;
  let jobs: JobicyJob[] = [];
  let total = 0;
  for (let p = 1; p <= opts.page; p++) {
    const res = await apiGet<SearchResponse>(buildUrl(opts, cursor));
    jobs = Array.isArray(res.jobs) ? res.jobs : [];
    total = typeof res.jobCount === "number" ? res.jobCount : jobs.length;
    cursor = res.nextCursor ?? undefined;
    if (!res.hasMore || !cursor) {
      if (p < opts.page) jobs = [];
      break;
    }
  }
  return { jobs, total };
}

/** Client-side refinement: --query AND --jobage over the fetched page. */
export function filterJobs(jobs: JobicyJob[], opts: Pick<SearchOpts, "query" | "jobage">): JobicyJob[] {
  return jobs.filter((j) => {
    if (opts.query && !matchesQuery(j, opts.query)) return false;
    if (opts.jobage < 9999) {
      const age = ageDays(j.pubDate);
      if (age === null || age > opts.jobage) return false;
    }
    return true;
  });
}

/** The date portion (YYYY-MM-DD) of an ISO timestamp, or "—" when absent. */
function shortDate(date: string | null): string {
  return date ? date.slice(0, 10) : "—";
}

// Table columns: header, width, and the cell value. The ID column is sized to
// the longest id so it is never truncated — a cut id can't be looked up in
// `detail`; the fixed-width columns truncate for scanning.
interface Column {
  header: string;
  width: number;
  value: (r: JobResult) => string;
}

function renderTable(rows: JobResult[]): string {
  const idWidth = Math.max(2, ...rows.map((r) => r.id.length));
  const cols: Column[] = [
    { header: "ID", width: idWidth, value: (r) => r.id },
    { header: "TITLE", width: 44, value: (r) => r.title },
    { header: "COMPANY", width: 20, value: (r) => r.company ?? "—" },
    { header: "LOCATION", width: 18, value: (r) => r.location ?? "—" },
    { header: "POSTED", width: 10, value: (r) => shortDate(r.date) },
  ];
  const cell = (s: string, w: number) => (s.length > w ? s.slice(0, w - 1) + "…" : s.padEnd(w));
  const lines = [cols.map((c) => cell(c.header, c.width)).join(" ")];
  for (const r of rows) lines.push(cols.map((c) => cell(c.value(r), c.width)).join(" "));
  return lines.join("\n");
}

function renderPlain(rows: JobResult[]): string {
  return rows.map((r) => `${r.title} — ${r.company ?? "—"} (${shortDate(r.date)})\n${r.url}`).join("\n\n");
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  try {
    const { jobs, total } = await fetchPage(opts);
    const filtered = filterJobs(jobs, opts);
    const page = filtered.slice(0, opts.limit);

    const results: JobResult[] = page.map((j) => {
      const r = toResult(j, opts.descriptionFormat);
      if (!opts.includeDescription) r.description = null;
      return r;
    });

    if (opts.format === "table") {
      process.stdout.write(renderTable(results) + (results.length ? "\n" : ""));
    } else if (opts.format === "plain") {
      process.stdout.write(renderPlain(results) + (results.length ? "\n" : ""));
    } else {
      process.stdout.write(JSON.stringify({ meta: { count: results.length, page: opts.page, total }, results }, null, 2) + "\n");
    }
    return 0;
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "SEARCH_FAILED");
    return 1;
  }
}
