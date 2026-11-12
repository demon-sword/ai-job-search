import {
  API_URL,
  ageDays,
  apiGet,
  slugifyCategory,
  toResult,
  writeError,
  type FeedResponse,
  type JobResult,
  type RemotiveJob,
} from "../helpers.js";

export interface SearchOpts {
  query?: string;
  categories: string[];
  jobage: number;
  page: number;
  limit: number;
  format: "json" | "table" | "plain";
  descriptionFormat: "markdown" | "text";
  includeDescription: boolean;
}

/** True when the job matches every query token (AND) across searchable text. */
export function matchesQuery(j: RemotiveJob, query: string): boolean {
  const hay = [j.title, j.company_name, j.category, ...(j.tags ?? []), j.description ?? ""]
    .join("\n")
    .toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .every((tok) => hay.includes(tok));
}

/** True when the job's category matches one of the requested slugs/labels. */
export function matchesCategory(j: RemotiveJob, categories: string[]): boolean {
  if (categories.length === 0) return true;
  const label = (j.category ?? "").toLowerCase();
  const slug = slugifyCategory(j.category ?? "");
  return categories.some((c) => {
    const want = c.toLowerCase();
    return label === want || slug === want || label.includes(want);
  });
}

/** Whole-feed fetch, then client-side filter (the API ignores query params). */
export function filterJobs(
  jobs: RemotiveJob[],
  opts: Pick<SearchOpts, "query" | "categories" | "jobage">,
): RemotiveJob[] {
  return jobs.filter((j) => {
    if (opts.query && !matchesQuery(j, opts.query)) return false;
    if (!matchesCategory(j, opts.categories)) return false;
    if (opts.jobage < 9999) {
      const age = ageDays(j.publication_date);
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
    const feed = await apiGet<FeedResponse>(API_URL);
    const jobs = Array.isArray(feed.jobs) ? feed.jobs : [];
    const filtered = filterJobs(jobs, opts);
    const total = filtered.length;
    const start = (opts.page - 1) * opts.limit;
    const page = filtered.slice(start, start + opts.limit);

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
