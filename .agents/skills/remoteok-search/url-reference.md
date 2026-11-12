# RemoteOK API reference

The endpoint and response shape this skill depends on. This is the file to
update if the RemoteOK API changes. Base: `https://remoteok.com`.

Verified against the live API (2026-11-12).

## Authentication

None for reads. The public API is offered for reuse subject to the attribution
terms noted in SKILL.md (link back with follow + mention RemoteOK as a source;
no logo use).

## `GET /api`

The only endpoint the skill uses, for both `search` (whole feed, filtered
client-side) and `detail` (find by id/slug within the feed). An optional
`?tag=<tag>` server-side filter exists; the CLI does not use it — one fetch
plus client-side `--query` filtering keeps the flag surface identical across
the feed-based skills in this repo.

The response is a JSON **array** whose first element is a legal-notice object
(no `id`); every later element is a job. The CLI skips entries without an `id`.

### Job object (the fields the skill reads)

```jsonc
{
  "id": "1137714",                       // string; -> result.id, and detail's <id>
  "slug": "remote-data-engineer-addepto-1137714",
  "epoch": 1794326404,
  "date": "2026-11-10T16:00:04+00:00",   // -> result.date (nullable)
  "company": "Addepto",                  // -> result.company
  "position": "Data Engineer",           // -> result.title
  "tags": ["design", "saas", ...],       // matched by --query
  "description": "<p>...</p>",           // HTML; cleaned client-side by cleanHtml
  "location": "",                        // often empty upstream -> null
  "url": "https://remoteOK.com/remote-jobs/...",  // canonical posting URL (kept verbatim)
  "apply_url": "https://...",            // direct apply link (surfaced in detail)
  "salary_min": 0, "salary_max": 0,      // 0 means undisclosed -> null
  "verified": false,
  "company_logo": "...", "logo": "..."
}
```

## Category filtering

RemoteOK has no category taxonomy in this endpoint, so there is no `--category`
flag: category-like narrowing goes through `--query`, which matches tags as
well as titles.

## Parsing notes

- The response is JSON, so there is no HTML card parsing. The only markup
  handling is `cleanHtml` (`cli/src/helpers.ts`): block tags become newlines,
  entities are decoded, tags stripped.
- Fetch uses `Mozilla/5.0 (compatible; remoteok-cli/1.0)`,
  `Accept: application/json`, and exponential backoff with jitter on 429/5xx
  (max 6 attempts). A connection error fails fast with a clear message.
