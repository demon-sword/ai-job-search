// Data source: Jobicy's public jobs API (JSON, `{jobs, nextCursor, ...}`).
// Reads are unauthenticated. Server-side filters (tag/geo/industry) narrow the
// 7-day feed window; --query refinement is client-side in commands/search.ts.

export const API_BASE = "https://jobicy.com/api/v2/remote-jobs";
export const STATUS_URL = "https://jobicy.com/api/jobs/status";

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n");
}

const UA = "Mozilla/5.0 (compatible; jobicy-cli/1.0)";

/** GET JSON with exponential backoff + jitter on 429/5xx (max 6 attempts). */
export async function apiGet<T>(url: string): Promise<T> {
  let last = "";
  for (let attempt = 0; attempt < 6; attempt++) {
    let res: Response;
    try {
      res = await fetch(url, {
        headers: { "User-Agent": UA, Accept: "application/json" },
      });
    } catch (e) {
      throw new Error(`request failed: ${e instanceof Error ? e.message : String(e)}`);
    }
    if (res.status === 429 || res.status >= 500) {
      last = `HTTP ${res.status}`;
      const { promise, resolve } = Promise.withResolvers<void>();
      setTimeout(resolve, 500 * 2 ** attempt + Math.random() * 200);
      await promise;
      continue;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return (await res.json()) as T;
  }
  throw new Error(`Jobicy API unreachable after retries (${last})`);
}

/** A Jobicy job — the fields this skill reads (the wire shape carries more). */
export interface JobicyJob {
  id: number;
  url: string;
  jobSlug: string;
  jobTitle: string;
  companyName: string;
  jobGeo: string;
  jobType: string[];
  jobLevel: string;
  jobIndustry: string[];
  jobExcerpt: string | null;
  jobDescription: string | null;
  pubDate: string | null;
  salaryMin: number | null;
  salaryCurrency: string | null;
  salaryPeriod: string | null;
}

export interface SearchResponse {
  jobs: JobicyJob[];
  jobCount: number;
  nextCursor: string | null;
  hasMore: boolean;
}

export type JobStatus = "active" | "closed" | "unknown";

/**
 * A search result in the portal-skill contract shape. `id` is Jobicy's numeric
 * job id, stringified — pass it as-is to `detail`.
 */
export interface JobResult {
  id: string;
  title: string;
  company: string | null;
  location: string | null;
  date: string | null;
  url: string;
  description: string | null;
}

/** A job detail: the search result plus status and enrichment. */
export interface JobDetailResult extends JobResult {
  status: JobStatus;
  level: string | null;
  salary: string | null;
}

/** Reshape a Jobicy job into the contract search-result fields. */
export function toResult(j: JobicyJob, descriptionFormat: "markdown" | "text" = "markdown"): JobResult {
  return {
    id: String(j.id),
    title: j.jobTitle,
    company: j.companyName || null,
    location: j.jobGeo || null,
    date: j.pubDate || null,
    url: j.url,
    description: descriptionFormat === "text" ? toText(j.jobDescription) : cleanHtml(j.jobDescription),
  };
}

/** Reshape a Jobicy job into the detail result (adds status + enrichment). */
export function toDetail(j: JobicyJob, status: JobStatus = "unknown"): JobDetailResult {
  return {
    ...toResult(j),
    status,
    level: j.jobLevel || null,
    salary: formatSalary(j.salaryMin, j.salaryCurrency, j.salaryPeriod),
  };
}

function formatSalary(min: number | null, currency: string | null, period: string | null): string | null {
  if (!min) return null;
  const per = period ? `/${period}` : "";
  return `${currency ? currency + " " : ""}${min}${per}`;
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
  hellip: "…",
  ndash: "–",
  mdash: "—",
};

function decodeHtmlEntities(text: string): string {
  return text.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (m, ent: string) => {
    if (ent[0] === "#") {
      const cp = ent[1]?.toLowerCase() === "x" ? parseInt(ent.slice(2), 16) : parseInt(ent.slice(1), 10);
      return cp >= 0 && cp <= 0x10ffff ? String.fromCodePoint(cp) : m;
    }
    return NAMED_ENTITIES[ent] ?? m;
  });
}

/**
 * Strip a posting's HTML into readable Markdown-ish prose: block/line-break
 * tags become newlines, list items become bullets, entities are decoded, tags
 * removed. Null for empty input.
 */
export function cleanHtml(html: string | null | undefined): string | null {
  if (!html) return null;
  const text = html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|h[1-6]|tr|table|ul|ol)>/gi, "\n")
    .replace(/<li[^>]*>/gi, "\n- ")
    .replace(/<\/li>/gi, "")
    .replace(/<[^>]+>/g, "");
  const collapsed = decodeHtmlEntities(text)
    .split("\n")
    .map((l) => l.replace(/[ \t]+/g, " ").trimEnd())
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return collapsed || null;
}

/** Plain-text variant: cleanHtml with bullets/headings flattened to paragraphs. */
export function toText(html: string | null | undefined): string | null {
  const md = cleanHtml(html);
  if (!md) return null;
  const flat = md
    .split("\n")
    .map((l) => l.replace(/^-\s+/, "").replace(/^#+\s+/, ""))
    .join("\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  return flat || null;
}

/** Extract a numeric Jobicy job id from a bare id, a slug, or a posting URL. */
export function normalizeId(input: string): string | null {
  const t = input.trim();
  if (/^\d+$/.test(t)) return t;
  const m = t.match(/(?:jobs\/)?(\d+)(?:-[a-z0-9-]*)?\/?(?:[?#].*)?$/i);
  return m ? m[1] : null;
}

/** Whole days between now and an ISO timestamp; null when unparseable. */
export function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 86_400_000;
}
