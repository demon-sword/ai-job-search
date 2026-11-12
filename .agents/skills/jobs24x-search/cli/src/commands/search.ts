import { ageDays, fetchListingPage, toResult, writeError, type JobResult } from "../helpers.js";

export interface SearchOpts {
  query?: string;
  remote?: string;
  employmentType?: string;
  country?: string;
  jobage: number;
  page: number;
  limit: number;
  format: "json" | "table" | "plain";
}

/** Client-side recency filter (--jobage over the returned page). */
export function filterJobs(jobs: JobResult[], jobage: number): JobResult[] {
  if (jobage >= 9999) return jobs;
  return jobs.filter((j) => {
    const age = ageDays(j.date);
    return age !== null && age <= jobage;
  });
}

/** The date portion (YYYY-MM-DD) of an ISO/date-only stamp, or "—". */
function shortDate(date: string | null): string {
  return date ? date.slice(0, 10) : "—";
}

function displayTitle(r: JobResult): string {
  return r.locked ? `${r.title} (locked)` : r.title;
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
    { header: "TITLE", width: 44, value: (r) => displayTitle(r) },
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
  return rows
    .map((r) => `${displayTitle(r)} — ${r.company ?? "no company (locked)"} (${shortDate(r.date)})\n${r.url ?? "no URL (locked)"}`)
    .join("\n\n");
}

export async function runSearch(opts: SearchOpts): Promise<number> {
  try {
    const params: Record<string, string> = { page: String(opts.page) };
    if (opts.query) params.query = opts.query;
    if (opts.remote) params.remote = opts.remote;
    if (opts.employmentType) params.employment_type = opts.employmentType;
    if (opts.country) params.country = opts.country;
    const listing = await fetchListingPage(params);
    const results = filterJobs(listing.jobs.map(toResult).slice(0, opts.limit), opts.jobage);
    const locked = listing.locked;

    if (opts.format === "table") {
      process.stdout.write(renderTable(results) + (results.length ? "\n" : ""));
    } else if (opts.format === "plain") {
      process.stdout.write(renderPlain(results) + (results.length ? "\n" : ""));
    } else {
      process.stdout.write(
        JSON.stringify({ meta: { count: results.length, page: listing.page, total: listing.total, locked }, results }, null, 2) + "\n",
      );
    }
    return 0;
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "SEARCH_FAILED");
    return 1;
  }
}
