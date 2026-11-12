// Data source: Working Nomads' public jobs feed (a JSON array).
// Reads are unauthenticated. The whole feed arrives in one call (the
// authenticated search API is deliberately unused), so filtering happens
// client-side in commands/search.ts.

export const FEED_URL = "https://www.workingnomads.com/api/exposed_jobs/";

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n");
}

const UA = "Mozilla/5.0 (compatible; working-nomads-cli/1.0)";

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
  throw new Error(`Working Nomads feed unreachable after retries (${last})`);
}

/** A Working Nomads job — the fields this skill reads. */
export interface WorkingNomadsJob {
  url: string;
  title: string;
  description: string | null;
  company_name: string;
  category_name: string;
  location: string;
  tags: string;
  pub_date: string | null;
}

/**
 * A search result in the portal-skill contract shape. `id` is the numeric id
 * from the posting's `/job/go/<id>/` URL — pass it as-is to `detail`.
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

/** A job detail: the search result plus category and tags. */
export interface JobDetailResult extends JobResult {
  category: string | null;
  tags: string[];
}

/** Reshape a feed job into the contract search-result fields. */
export function toResult(j: WorkingNomadsJob, descriptionFormat: "markdown" | "text" = "markdown"): JobResult {
  return {
    id: extractId(j.url) ?? j.url,
    title: j.title,
    company: j.company_name || null,
    location: j.location || null,
    date: j.pub_date || null,
    url: j.url,
    description: descriptionFormat === "text" ? toText(j.description) : cleanHtml(j.description),
  };
}

/** Reshape a feed job into the detail result (adds category + split tags). */
export function toDetail(j: WorkingNomadsJob): JobDetailResult {
  return {
    ...toResult(j),
    category: j.category_name || null,
    tags: splitTags(j.tags),
  };
}

/** Upstream tags are one comma-separated string; split into a trimmed list. */
export function splitTags(csv: string | null | undefined): string[] {
  if (!csv) return [];
  return csv
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
}

/** Numeric id from a /job/go/<id>/ URL; null when the URL has no id tail. */
export function extractId(url: string): string | null {
  const m = url.match(/\/(\d+)\/?(?:[?#].*)?$/);
  return m ? m[1] : null;
}

/** Accept a bare id or a /job/go/<id>/ URL. */
export function normalizeId(input: string): string | null {
  const t = input.trim();
  if (/^\d+$/.test(t)) return t;
  return extractId(t);
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

/** Whole days between now and an ISO timestamp; null when unparseable. */
export function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 86_400_000;
}
