// Data source: jobs24x's own Supabase edge functions (the same calls the
// public site makes for anonymous visitors). The payload is AES-GCM
// encrypted; the session key comes from the key endpoint (works anonymously).
// Reads need no account. The anonymous payload is LOCKED (no company/URL) —
// see SKILL.md's Auth gap; a user-supplied JOBS24X_API_TOKEN is passed through
// as the bearer header for paid sessions.

export const SUPABASE_PROJECT = "nuzenytiidpypvixcjet";
export const SESSION_KEY_PATH = "/functions/v1/session-key";
export const LIST_JOBS_PATH = "/functions/v1/list-jobs";

/**
 * The site's PUBLIC anon key, shipped in its JS bundle for every visitor
 * (Supabase anon keys are public by design; access is gated server-side).
 * Not a secret, not an account. Rotation procedure: see url-reference.md.
 */
export const PUBLIC_ANON_KEY =
  "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im51emVueXRpaWRweXB2aXhjamV0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQyNTIwNDUsImV4cCI6MjA4OTgyODA0NX0.CWRO3ysv3xtnDN9PlPhs9Y3Dv4FNoySIhjzHbvlb84k";

export function writeError(error: string, code: string): void {
  process.stderr.write(JSON.stringify({ error, code }) + "\n");
}

const UA = "Mozilla/5.0 (compatible; jobs24x-cli/1.0)";

/** GET with exponential backoff + jitter on 429/5xx (max 6 attempts). */
async function fetchWithRetry(url: string, headers: Record<string, string>): Promise<Response> {
  let last = "";
  for (let attempt = 0; attempt < 6; attempt++) {
    let res: Response;
    try {
      res = await fetch(url, { headers: { "User-Agent": UA, ...headers } });
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
    return res;
  }
  throw new Error(`jobs24x unreachable after retries (${last})`);
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    apikey: PUBLIC_ANON_KEY,
    "Content-Type": "application/json",
  };
  // Paid-session passthrough (user's own token, env only — never a flag).
  const token = process.env.JOBS24X_API_TOKEN;
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

function base64ToBytes(s: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(Buffer.from(s, "base64"));
}

interface SessionKey {
  key: CryptoKey;
  audience: string;
}

/** Fetch the AES-GCM session key (works anonymously; bearer added when set). */
export async function fetchSessionKey(): Promise<SessionKey> {
  const url = `https://${SUPABASE_PROJECT}.supabase.co${SESSION_KEY_PATH}`;
  const res = await fetchWithRetry(url, authHeaders());
  if (!res.ok) throw new Error(`session-key HTTP ${res.status}`);
  const body = (await res.json()) as { key?: string; audience?: string };
  if (!body.key || !body.audience) throw new Error("session-key response missing key/audience");
  const key = await crypto.subtle.importKey("raw", base64ToBytes(body.key), { name: "AES-GCM" }, false, [
    "decrypt",
  ]);
  return { key, audience: body.audience };
}

/** A jobs24x job — the fields this skill reads. */
export interface Jobs24xJob {
  id: string;
  title: string;
  description: string | null;
  location: string | null;
  remote_status: string | null;
  employment_type: string | null;
  date_posted: string | null;
  url: string;
  companies: { name: string | null } | null;
  locked: boolean;
}

interface ListJobsData {
  jobs: Jobs24xJob[];
  page: number;
  totalPages: number;
  total: number;
  isPaid: boolean;
}

export interface ListingPage {
  jobs: Jobs24xJob[];
  page: number;
  totalPages: number;
  total: number;
  locked: boolean;
}

/** Fetch and decrypt one listing page. `locked` is true while unpaid. */
export async function fetchListingPage(params: Record<string, string>): Promise<ListingPage> {
  const { key, audience } = await fetchSessionKey();
  const query = new URLSearchParams(params);
  const url = `https://${SUPABASE_PROJECT}.supabase.co${LIST_JOBS_PATH}?${query.toString()}`;
  const res = await fetchWithRetry(url, authHeaders());
  if (!res.ok) throw new Error(`list-jobs HTTP ${res.status}`);
  const env = (await res.json()) as { v?: number; iv?: string; ct?: string };
  if (env.v !== 1 || !env.iv || !env.ct) {
    // Unencrypted payload (or an error shape): pass through if it has data.
    const raw = env as unknown as { data?: ListJobsData };
    if (raw.data && Array.isArray(raw.data.jobs)) {
      return { jobs: raw.data.jobs, page: raw.data.page, totalPages: raw.data.totalPages, total: raw.data.total, locked: !raw.data.isPaid };
    }
    throw new Error("unexpected list-jobs response envelope");
  }
  const pt = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: base64ToBytes(env.iv), additionalData: new TextEncoder().encode(audience) },
    key,
    base64ToBytes(env.ct),
  );
  const data = JSON.parse(new TextDecoder().decode(pt)) as { data: ListJobsData };
  return {
    jobs: data.data.jobs,
    page: data.data.page,
    totalPages: data.data.totalPages,
    total: data.data.total,
    locked: !data.data.isPaid,
  };
}

/**
 * A search result in the portal-skill contract shape. `company` and `url` are
 * null while the row is locked (anonymous free surface); `locked` says so.
 */
export interface JobResult {
  id: string;
  title: string;
  company: string | null;
  location: string | null;
  date: string | null;
  url: string | null;
  description: string | null;
  locked: boolean;
}

/** Reshape a jobs24x job into the contract search-result fields. */
export function toResult(j: Jobs24xJob): JobResult {
  const locked = j.locked || j.url === "#locked";
  return {
    id: j.id,
    title: j.title,
    company: locked ? null : j.companies?.name ?? null,
    location: [j.location, j.remote_status].filter(Boolean).join(" · ") || null,
    date: j.date_posted || null,
    url: locked ? null : j.url || null,
    description: j.description || null,
    locked,
  };
}

/** Whole days between now and a YYYY-MM-DD / ISO date; null when unparseable. */
export function ageDays(iso: string | null): number | null {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 86_400_000;
}

export const UNLOCK_NOTE =
  "jobs24x locks company names and apply URLs behind a paid pass: set JOBS24X_API_TOKEN (your own paid session token) and flip enabled: true in SKILL.md — see Auth gap.";
