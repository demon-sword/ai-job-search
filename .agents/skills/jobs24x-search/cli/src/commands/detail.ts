import { UNLOCK_NOTE, fetchListingPage, toResult, writeError, type JobResult } from "../helpers.js";

export interface DetailOpts {
  id: string; // a jobs24x posting UUID
  format: "json" | "plain";
  maxPages: number;
  // Replay the listing query that surfaced the UUID (list-jobs has no
  // per-job endpoint; a UUID is only findable inside its own filtered
  // listing — the unfiltered feed is ~11k jobs deep).
  query?: string;
  remote?: string;
  employmentType?: string;
  country?: string;
}

/** A human-readable rendering of one locked record plus the unlock note. */
function renderPlain(job: JobResult): string {
  const lines = [
    `${job.title}${job.locked ? " (locked)" : ""}`,
    `${job.company ?? "no company (locked)"} · ${job.location ?? "—"}`,
  ];
  if (job.date) lines.push(`Posted: ${job.date.slice(0, 10)}`);
  lines.push("", job.description ?? "(no description)", "", `URL: ${job.url ?? "no URL (locked)"}`, `id: ${job.id}`);
  if (job.locked) lines.push("", UNLOCK_NOTE);
  return lines.join("\n");
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function runDetail(opts: DetailOpts): Promise<number> {
  const uuid = opts.id.trim();
  if (!UUID_RE.test(uuid)) {
    writeError(`could not parse a jobs24x posting UUID from "${opts.id}"`, "BAD_ID");
    return 1;
  }
  try {
    // No per-job endpoint exists: replay the filtered listing (when the
    // caller passes the original --query) and walk pages until it appears.
    const params: Record<string, string> = {};
    if (opts.query) params.query = opts.query;
    if (opts.remote) params.remote = opts.remote;
    if (opts.employmentType) params.employment_type = opts.employmentType;
    if (opts.country) params.country = opts.country;
    let found = null;
    let locked = true;
    for (let page = 1; page <= opts.maxPages; page++) {
      const listing = await fetchListingPage({ ...params, page: String(page) });
      locked = listing.locked;
      found = listing.jobs.find((j) => j.id.toLowerCase() === uuid.toLowerCase()) ?? null;
      if (found) break;
      if (page >= listing.totalPages) break;
    }
    if (!found) {
      writeError(
        `job ${uuid} not found${opts.query ? ` for query "${opts.query}"` : " in the recent unfiltered listing"} — pass the --query the search used (list-jobs has no per-job lookup)`,
        "NOT_FOUND",
      );
      return 1;
    }
    const job = toResult(found);

    if (opts.format === "plain") {
      process.stdout.write(renderPlain(job) + "\n");
    } else {
      process.stdout.write(JSON.stringify({ ...job, unlock_note: locked ? UNLOCK_NOTE : null }, null, 2) + "\n");
    }
    return 0;
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "DETAIL_FAILED");
    return 1;
  }
}
