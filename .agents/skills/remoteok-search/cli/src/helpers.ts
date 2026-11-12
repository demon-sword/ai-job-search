// Data source: RemoteOK's public jobs API (a JSON array; the first element is
// a legal notice, not a job). Reads are unauthenticated. The whole feed
// arrives in one call, so filtering happens client-side in commands/search.ts.

export const API_URL = "https://remoteok.com/api";

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n");
}

const UA = "Mozilla/5.0 (compatible; remoteok-cli/1.0)";

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
  throw new Error(`RemoteOK API unreachable after retries (${last})`);
}

/** A RemoteOK job — the fields this skill reads (the wire shape carries more). */
export interface RemoteOKJob {
  id: string;
  slug: string;
  date: string | null;
  company: string;
  position: string;
  tags: string[];
  description: string | null;
  location: string;
  url: string;
  apply_url: string | null;
  salary_min: number;
  salary_max: number;
}

/**
 * A search result in the portal-skill contract shape. `id` is RemoteOK's job
 * id — pass it as-is to `detail` (a slug or posting URL works too).
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
  slug: string | null;
  apply_url: string | null;
  salary: string | null;
  tags: string[];
}

/** Reshape a RemoteOK job into the contract search-result fields. */
export function toResult(j: RemoteOKJob, descriptionFormat: "markdown" | "text" = "markdown"): JobResult {
  return {
    id: String(j.id),
    title: j.position,
    company: j.company || null,
    location: j.location || null,
    date: j.date || null,
    url: j.url,
    description: descriptionFormat === "text" ? toText(j.description) : cleanHtml(j.description),
  };
}

/** Reshape a RemoteOK job into the detail result (adds enrichment). */
export function toDetail(j: RemoteOKJob): JobDetailResult {
  return {
    ...toResult(j),
    slug: j.slug || null,
    apply_url: j.apply_url || null,
    salary: formatSalary(j.salary_min, j.salary_max),
    tags: Array.isArray(j.tags) ? j.tags : [],
  };
}

/** "0" upstream means undisclosed; render a range only when something is set. */
export function formatSalary(min: number, max: number): string | null {
  if (!min && !max) return null;
  if (min && max) return min === max ? `$${min}` : `$${min} - $${max}`;
  return `$${min || max}`;
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

/** Extract a RemoteOK job id from a bare id, a slug, or a posting URL. */
export function normalizeId(input: string): string | null {
  const t = input.trim();
  if (/^\d+$/.test(t)) return t;
  const slugTail = t.match(/([a-z0-9-]+-\d+)\/?(?:[?#].*)?$/i);
  if (slugTail) {
    const idTail = slugTail[1].match(/-(\d+)$/);
    return idTail ? idTail[1] : null;
  }
  return null;
}

/** Whole days between now and an ISO timestamp; null when unparseable. */
export function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 86_400_000;
}
