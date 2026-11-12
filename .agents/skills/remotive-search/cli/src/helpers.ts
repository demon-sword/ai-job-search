// Data source: Remotive's public jobs API (JSON, `{jobs: [...]}`).
// Reads are unauthenticated. The endpoint ignores query parameters and returns
// the whole feed, so filtering happens client-side in commands/search.ts.

export const API_URL = "https://remotive.com/api/remote-jobs";

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n");
}

const UA = "Mozilla/5.0 (compatible; remotive-cli/1.0)";

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
  throw new Error(`Remotive API unreachable after retries (${last})`);
}


/** A Remotive job — the fields this skill reads (the wire shape carries more). */
export interface RemotiveJob {
  id: number;
  url: string;
  title: string;
  company_name: string;
  category: string;
  tags: string[];
  job_type: string;
  publication_date: string | null;
  candidate_required_location: string;
  salary: string | null;
  description: string | null;
}

export interface FeedResponse {
  jobs: RemotiveJob[];
  "job-count"?: number;
}

/**
 * A search result in the portal-skill contract shape. `id` is Remotive's
 * numeric job id, stringified — pass it as-is to `detail`.
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

/** A job detail: the search result plus enrichment the feed already carries. */
export interface JobDetailResult extends JobResult {
  category: string | null;
  job_type: string | null;
  salary: string | null;
  tags: string[];
}

/** Reshape a Remotive job into the contract search-result fields. */
export function toResult(j: RemotiveJob, descriptionFormat: "markdown" | "text" = "markdown"): JobResult {
  return {
    id: String(j.id),
    title: j.title,
    company: j.company_name || null,
    location: j.candidate_required_location || null,
    date: j.publication_date || null,
    url: j.url,
    description: descriptionFormat === "text" ? toText(j.description) : cleanHtml(j.description),
  };
}

/** Reshape a Remotive job into the detail result (adds enrichment). */
export function toDetail(j: RemotiveJob): JobDetailResult {
  return {
    ...toResult(j),
    category: j.category || null,
    job_type: j.job_type || null,
    salary: j.salary || null,
    tags: Array.isArray(j.tags) ? j.tags : [],
  };
}

/** Remotive's label ("Software Development") -> slug ("software-dev"). */
export function slugifyCategory(label: string): string {
  return label
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/development$/, "dev");
}

const NAMED_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
  nbsp: " ",
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

/** Extract a numeric Remotive job id from a bare id or a /remote-jobs/... URL. */
export function normalizeId(input: string): string | null {
  const bare = input.trim().match(/^(\d+)$/);
  if (bare) return bare[1];
  const fromUrl = input.match(/-(\d+)\/?(?:[?#].*)?$/);
  return fromUrl ? fromUrl[1] : null;
}

/** Whole days between now and an ISO timestamp; null when unparseable. */
export function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 86_400_000;
}
