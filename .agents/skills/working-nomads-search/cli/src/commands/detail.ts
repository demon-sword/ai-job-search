import { FEED_URL, apiGet, normalizeId, toDetail, writeError, type JobDetailResult, type WorkingNomadsJob } from "../helpers.js";

export interface DetailOpts {
  id: string; // a Working Nomads numeric job id or a /job/go/<id>/ URL
  format: "json" | "plain";
}

/** A human-readable rendering of one job: header, present fields, description. */
function renderPlain(job: JobDetailResult): string {
  const lines = [job.title, `${job.company ?? "—"} · ${job.location ?? "—"}`];

  const field = (label: string, value: string | null) => {
    if (value) lines.push(`${label}: ${value}`);
  };
  field("Posted", job.date && job.date.slice(0, 10));
  field("Category", job.category);
  if (job.tags.length) lines.push(`Tags: ${job.tags.join(", ")}`);

  lines.push("", job.description ?? "(no description)", "", `URL: ${job.url}`, `id: ${job.id}`);
  return lines.join("\n");
}

export async function runDetail(opts: DetailOpts): Promise<number> {
  const id = normalizeId(opts.id);
  if (!id) {
    writeError(`could not parse a Working Nomads job id from "${opts.id}"`, "BAD_ID");
    return 1;
  }
  try {
    // No per-job endpoint exists: find the id in the current feed.
    const feed = await apiGet<WorkingNomadsJob[]>(FEED_URL);
    const found = (Array.isArray(feed) ? feed : []).find((j) => normalizeId(j.url) === id);
    if (!found) {
      writeError(`job ${id} is not in the current feed (it may have closed or aged out)`, "NOT_FOUND");
      return 1;
    }
    const job = toDetail(found);

    if (opts.format === "plain") {
      process.stdout.write(renderPlain(job) + "\n");
    } else {
      process.stdout.write(JSON.stringify(job, null, 2) + "\n");
    }
    return 0;
  } catch (e) {
    writeError(e instanceof Error ? e.message : String(e), "DETAIL_FAILED");
    return 1;
  }
}
