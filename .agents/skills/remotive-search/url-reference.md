# Remotive API reference

The endpoint and response shape this skill depends on. This is the file to
update if the Remotive API changes. Base: `https://remotive.com`.

Verified against the live API (2026-11-12).

## Authentication

None for reads. The public API is granted so developers can share jobs further,
subject to the attribution terms noted in SKILL.md (link back + mention
Remotive as a source; do not submit jobs to third-party job sites).

## `GET /api/remote-jobs`

The only endpoint the skill uses, for both `search` (whole feed, filtered
client-side) and `detail` (find by id within the feed).

The endpoint **ignores query parameters** (verified 2026-11-12: `?search=...`,
`?category=...`, `?limit=500` all return a byte-identical body), so the CLI
sends a bare request and applies `--query` / `--category` / `--jobage` /
`--limit` / `--page` itself.

**Feed stub (2026-11-12): the API currently serves ~14 jobs**
(`job-count: 14, total-job-count: 14`) — a stub, not the browsable site's full
corpus. Do NOT assume full coverage from this endpoint: results are whatever
the stub window holds. Client-side filtering means the skill recovers
automatically if/when the API serves the full feed again — no code change
needed. The evaluated alternative, Remotive's RSS
(`/remote-jobs/rss-feed`), returns an HTML/JS shell with zero `<item>` nodes
and was rejected in favor of this endpoint.

### Job object (the fields the skill reads)

```jsonc
{
  "id": 2091171,                         // -> result.id (stringified), and detail's <id>
  "url": "https://remotive.com/remote-jobs/customer-service/...-2091171",
  "title": "Customer Service Representative",
  "company_name": "ShyftOff",            // -> result.company
  "company_logo": "https://remotive.com/job/2091171/logo",
  "category": "Customer Service",        // human label; --category matches slug or label
  "tags": ["video", "healthcare", ...],  // matched by --query
  "job_type": "contract",
  "publication_date": "2026-11-11T09:11:58",  // -> result.date (nullable)
  "candidate_required_location": "USA",  // -> result.location
  "salary": "$14 - $20/hour",
  "description": "<p>...</p>"            // HTML; cleaned client-side by cleanHtml
}
```

The numeric `id` is stable and appears both as `id` and as the URL suffix.

## Category slugs for `--category`

The API returns human-readable labels (`"Software Development"`); the CLI
matches `--category` case-insensitively against both the label and its
slugified form (`software-dev`), so `--category software-dev` and
`--category "Software Development"` both work. Known labels include Software
Development, Data, Marketing, Sales, Customer Service, DevOps, Design,
Product, Finance, HR, Writing, IT, QA.

## Parsing notes

- The response is JSON, so there is no HTML card parsing. The only markup
  handling is `cleanHtml` (`cli/src/helpers.ts`): block tags become newlines,
  entities are decoded, tags stripped.
- Search descriptions arrive in the same payload as the listing, passed through
  `cleanHtml` once — never cleaned twice.
- Fetch uses `Mozilla/5.0 (compatible; remotive-cli/1.0)`,
  `Accept: application/json`, and exponential backoff with jitter on 429/5xx
  (max 6 attempts). A connection error fails fast with a clear message.
- `robots.txt` disallows crawler paths such as `/jobs/*` — this skill does not
  crawl those paths; it uses the official public API, which is the sanctioned
  access method.
