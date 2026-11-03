// One command: live portal discovery + dedupe + full-text triage, then the
// ranked shortlist with pay-vs-bar and India-eligibility verdicts.
//
// Discovery reuses the installed portal-search CLIs under .agents/skills
// (each portal's own search/detail interface — flags are probed from its
// SKILL.md, never guessed). Dedupe reuses tools/job_key.py (never forked:
// called as a subprocess, with a local slug fallback if python is absent).
// Triage reuses each portal's `detail` command for full posting text; cached
// fetches and polite pacing keep portal load low, and a per-portal failure
// is recorded in portal_errors without failing the run.

import { createHash } from "crypto";
import { join } from "path";
import {
  buildReport,
  normalizeUrl,
  type Candidate,
  type CheckReport,
} from "../helpers.js";

export interface CheckOpts {
  query: string;
  location: string;
  jobage?: number;
  limit: number;
  portals?: string[];
  input?: string;
  cacheDir: string;
  noCache: boolean;
  payBarLpa: number;
  minScore: number;
}

// Layout anchor: <root>/.agents/skills/job-check/cli/src/commands/check.ts
const SKILL_DIR = join(import.meta.dir, "..", "..", "..");
const SKILLS_ROOT = join(SKILL_DIR, "..");
const REPO_ROOT = join(SKILL_DIR, "..", "..", "..");

const DETAIL_CACHE_TTL_MS = 7 * 24 * 3600 * 1000;
const PORTAL_PAUSE_MS = 1000;
const DETAIL_PAUSE_MS = 1000;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function runWithTimeout(
  argv: string[],
  timeoutMs: number
): Promise<{ stdout: string; exitCode: number }> {
  const proc = Bun.spawn(argv, { stdout: "pipe", stderr: "pipe" });
  const timer = setTimeout(() => proc.kill(), timeoutMs);
  try {
    const [stdout, exitCode] = await Promise.all([
      new Response(proc.stdout).text(),
      proc.exited,
    ]);
    return { stdout: stdout.trim(), exitCode };
  } finally {
    clearTimeout(timer);
  }
}

interface Portal {
  name: string;
  cliPath: string;
  skillDoc: string;
}

async function discoverPortals(filter?: string[]): Promise<Portal[]> {
  const portals: Portal[] = [];
  const entries = await Array.fromAsync(new Bun.Glob("*").scan({ cwd: SKILLS_ROOT, onlyFiles: false }));
  for (const name of entries.sort()) {
    if (name === "job-check") continue;
    if (filter && filter.length > 0 && !filter.includes(name)) continue;
    const skillPath = join(SKILLS_ROOT, name, "SKILL.md");
    const cliPath = join(SKILLS_ROOT, name, "cli", "src", "cli.ts");
    const skillFile = Bun.file(skillPath);
    const cliFile = Bun.file(cliPath);
    if (!(await skillFile.exists()) || !(await cliFile.exists())) continue;
    const doc = await skillFile.text();
    const frontmatter = doc.startsWith("---\n") ? doc.slice(4, doc.indexOf("\n---")) : "";
    if (/^enabled:\s*false/m.test(frontmatter)) continue;
    portals.push({ name, cliPath, skillDoc: doc });
  }
  return portals;
}

function supportsFlag(skillDoc: string, flag: string): boolean {
  return skillDoc.includes(flag);
}

interface SearchHit {
  title?: string;
  company?: string;
  location?: string;
  url?: string;
  date?: string;
  snippet?: string;
  description?: string;
}

function toCandidates(hits: SearchHit[], portal: string): Candidate[] {
  const out: Candidate[] = [];
  for (const h of hits) {
    if (!h.title || !h.url) continue;
    out.push({
      title: String(h.title),
      company: String(h.company ?? "Unknown"),
      location: String(h.location ?? ""),
      url: String(h.url),
      portal,
      date: h.date ? String(h.date) : undefined,
      snippet: h.snippet ? String(h.snippet) : undefined,
      text: h.description ? String(h.description) : undefined,
    });
  }
  return out;
}

async function searchPortal(
  portal: Portal,
  opts: CheckOpts
): Promise<{ candidates: Candidate[]; error?: string }> {
  const args = ["run", portal.cliPath, "search", "--query", opts.query, "--format", "json"];
  if (supportsFlag(portal.skillDoc, "--location")) {
    args.push("--location", opts.location);
  }
  if (opts.jobage !== undefined && supportsFlag(portal.skillDoc, "--jobage")) {
    args.push("--jobage", String(opts.jobage));
  }
  if (supportsFlag(portal.skillDoc, "--limit")) {
    args.push("--limit", String(opts.limit));
  }
  try {
    const { stdout, exitCode } = await runWithTimeout(["bun", ...args], 90000);
    if (exitCode !== 0) {
      return { candidates: [], error: `search exited ${exitCode}: ${stdout.slice(0, 200)}` };
    }
    const parsed = JSON.parse(stdout) as { results?: SearchHit[] } | SearchHit[];
    const hits = Array.isArray(parsed) ? parsed : (parsed.results ?? []);
    return { candidates: toCandidates(hits, portal.name) };
  } catch (err) {
    return { candidates: [], error: String(err).slice(0, 200) };
  }
}

async function jobKey(company: string, title: string): Promise<string> {
  // Reuse tools/job_key.py as-is; the local slug is only a fallback.
  try {
    const { stdout, exitCode } = await runWithTimeout(
      ["python3", join(REPO_ROOT, "tools", "job_key.py"), "--company", company, "--title", title],
      15000
    );
    if (exitCode === 0 && stdout) return stdout.split("\n").pop()!.trim() || localSlug(company, title);
  } catch {
    // fall through to the local slug
  }
  return localSlug(company, title);
}

function localSlug(company: string, title: string): string {
  const slug = (s: string) =>
    s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);
  return `${slug(company)}_${slug(title)}`;
}

function cachePath(cacheDir: string, url: string): string {
  const hash = createHash("sha256").update(url).digest("hex");
  return join(cacheDir, `${hash}.json`);
}

function extractDetailText(stdout: string): string | undefined {
  const trimmed = stdout.trim();
  if (!trimmed) return undefined;
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    for (const key of ["description", "full_description", "text", "body", "snippet"]) {
      const v = parsed[key];
      if (typeof v === "string" && v.trim().length >= 20) return v;
    }
    const nested = parsed["result"] ?? parsed["job"];
    if (nested && typeof nested === "object") {
      for (const key of ["description", "full_description", "text", "body", "snippet"]) {
        const v = (nested as Record<string, unknown>)[key];
        if (typeof v === "string" && v.trim().length >= 20) return v;
      }
    }
    return undefined;
  } catch {
    return trimmed.length >= 20 ? trimmed : undefined;
  }
}

async function fetchDetail(
  portal: Portal,
  candidate: Candidate,
  opts: CheckOpts
): Promise<void> {
  const path = cachePath(opts.cacheDir, candidate.url);
  if (!opts.noCache) {
    const cached = Bun.file(path);
    if (await cached.exists()) {
      try {
        const payload = (await cached.json()) as { fetched_at: number; stdout: string };
        if (Date.now() - payload.fetched_at < DETAIL_CACHE_TTL_MS) {
          const text = extractDetailText(payload.stdout);
          if (text) candidate.text = text;
          return;
        }
      } catch {
        // corrupt cache entry: refetch below
      }
    }
  }
  try {
    const { stdout, exitCode } = await runWithTimeout(
      ["bun", "run", portal.cliPath, "detail", candidate.url, "--format", "json"],
      60000
    );
    if (exitCode === 0) {
      if (!opts.noCache) {
        await Bun.write(path, JSON.stringify({ fetched_at: Date.now(), stdout }));
      }
      const text = extractDetailText(stdout);
      if (text) candidate.text = text;
    }
  } catch {
    // A failed fetch degrades the candidate to a stub; never fails the run.
  }
}

export async function runCheck(opts: CheckOpts): Promise<CheckReport> {
  if (opts.input) {
    const file = Bun.file(opts.input);
    if (!(await file.exists())) {
      throw new Error(`input file not found: ${opts.input}`);
    }
    const candidates = (await file.json()) as Candidate[];
    return buildReport(candidates, {
      payBarLpa: opts.payBarLpa,
      minScore: opts.minScore,
      query: opts.query,
    });
  }

  const portals = await discoverPortals(opts.portals);
  const report = buildReport([], {
    payBarLpa: opts.payBarLpa,
    minScore: opts.minScore,
    query: opts.query,
  });

  let pool: Candidate[] = [];
  for (const portal of portals) {
    const { candidates, error } = await searchPortal(portal, opts);
    pool.push(...candidates);
    if (error) report.portal_errors.push({ portal: portal.name, error });
    await sleep(PORTAL_PAUSE_MS);
  }

  // Dedupe: exact URL first, then the canonical job key.
  const seenUrls = new Set<string>();
  const seenKeys = new Set<string>();
  const deduped: Candidate[] = [];
  for (const c of pool) {
    const url = normalizeUrl(c.url);
    if (seenUrls.has(url)) continue;
    seenUrls.add(url);
    const key = await jobKey(c.company, c.title);
    if (seenKeys.has(key)) continue;
    seenKeys.add(key);
    deduped.push(c);
  }

  const shortlist = deduped.slice(0, opts.limit);
  const byPortal = new Map(portals.map((p) => [p.name, p]));
  for (const c of shortlist) {
    const portal = c.portal ? byPortal.get(c.portal) : undefined;
    if (portal && !(c.text ?? "").trim()) {
      await fetchDetail(portal, c, opts);
      await sleep(DETAIL_PAUSE_MS);
    }
  }

  const full = buildReport(shortlist, {
    payBarLpa: opts.payBarLpa,
    minScore: opts.minScore,
    query: opts.query,
  });
  full.portal_errors.unshift(...report.portal_errors);
  return full;
}
