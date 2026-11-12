import { API_BASE, STATUS_URL, apiGet, normalizeId, toDetail, writeError, type JobDetailResult, type JobStatus, type JobicyJob, type SearchResponse } from "../helpers.js";

export interface DetailOpts {
  id: string; // a Jobicy numeric job id, slug, or posting URL
  format: "json" | "plain";
}

/** A human-readable rendering of one job: header, present fields, description. */
function renderPlain(job: JobDetailResult): string {
  const lines = [job.title, `${job.company ?? "—"} · ${job.location ?? "—"}`];

  const field = (label: string, value: string | null) => {
    if (value) lines.push(`${label}: ${value}`);
  };
  field("Posted", job.date && job.date.slice(0, 10));
  field("Status", job.status);
  field("Level", job.level);
  field("Salary", job.salary);

  lines.push("", job.description ?? "(no description)", "", `URL: ${job.url}`, `id: ${job.id}`);
  return lines.join("\n");
}

interface StatusResponse {
  jobs?: Array<{ id: number; status?: string }>;
}

function coerceStatus(raw: string | undefined): JobStatus {
  return raw === "active" || raw === "closed" ? raw : "unknown";
}

export async function runDetail(opts: DetailOpts): Promise<number> {
  const id = normalizeId(opts.id);
  if (!id) {
    writeError(`could not parse a Jobicy job id from "${opts.id}"`, "BAD_ID");
    return 1;
  }
  try {
    // No per-job endpoint exists: find the id in the 7-day feed window.
    const res = await apiGet<SearchResponse>(`${API_BASE}?count=100`);
    const found = (Array.isArray(res.jobs) ? res.jobs : []).find((j) => String(j.id) === id);
    if (!found) {
      writeError(`job ${id} is not in the current 7-day feed window (it may have closed or aged out)`, "NOT_FOUND");
      return 1;
    }
    // Live open/closed status for the one id.
    let status: JobStatus = "unknown";
    try {
      const st = await apiGet<StatusResponse>(`${STATUS_URL}?ids=${encodeURIComponent(id)}`);
      const row = st.jobs?.find((s) => String(s.id) === id);
      status = coerceStatus(row?.status);
    } catch {
      status = "unknown";
    }
    const job = toDetail(found, status);

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
