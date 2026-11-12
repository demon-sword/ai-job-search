# Jobicy API reference

The endpoints, parameters, and response shapes this skill depends on. This is
the file to update if the Jobicy API changes. Base: `https://jobicy.com`.

Verified against the live API (2026-11-12; API v2.2.19).

## Authentication

None for reads. Without an API key, `url` always contains the Jobicy listing
URL and the public API remains free — this skill uses exactly that mode and
never touches the commercial API (direct ATS links).

## `GET /api/v2/remote-jobs`

The endpoint the skill's `search` command uses. Returns matching listings
published in the **last 7 days**, with cursor pagination (`nextCursor`,
`hasMore`). `jobCount` is the match count for the current filter set.

Query parameters used by the skill:

| Param | Maps to CLI flag | Notes |
|-------|------------------|-------|
| `count` | `--limit` | Page size, up to 100. |
| `cursor` | (derived) | `nextCursor` from the previous page implements `--page`. |
| `tag` | `--tag` | Keyword filter applied to the job title (e.g. `python`, `marketing`). |
| `geo` | `--geo` | Country filter (e.g. `canada`, `usa`) — lowercase slugs. |
| `industry` | `--industry` | Category filter (e.g. `engineering`, `supporting`). |

There is no free-text `search` parameter (the API answers 400
`Unexpected parameter 'search'`); `--query` is therefore refined client-side
against title, company, excerpt, and description.

### Job object (the fields the skill reads)

```jsonc
{
  "id": 161642,                                  // -> result.id (stringified), detail's <id>
  "url": "https://jobicy.com/jobs/161642-...",   // the Jobicy listing URL
  "jobSlug": "161642-forward-deployed-engineer-10",
  "jobTitle": "Forward Deployed Engineer",       // -> result.title
  "companyName": "Nearform",                     // -> result.company
  "companyLogo": "https://jobicy.com/data/...",
  "jobIndustry": ["Software Engineering"],
  "jobType": ["Full-Time"],
  "jobGeo": "Italy",                             // -> result.location
  "jobLevel": "Senior",
  "jobExcerpt": "This is a full-time, ...",     // plain-text snippet (entities unescaped client-side)
  "jobDescription": "<p>...</p>",               // HTML; cleaned client-side by cleanHtml
  "pubDate": "2026-11-12T13:29:03+00:00",        // -> result.date (nullable)
  "salaryMin": null, "salaryCurrency": "USD", "salaryPeriod": null
}
```

## `GET /api/jobs/status?ids=<id>,...`

Batch status check. Returns per-id `active` / `closed` / `unknown`. The
skill's `detail` command calls it for the requested id and surfaces the value
as `status` — `active` means the posting is still listed, `closed` means it is
gone (mark expired, don't silently drop), `unknown` means no record.

## Parsing notes

- The response is JSON, so there is no HTML card parsing. The only markup
  handling is `cleanHtml` (`cli/src/helpers.ts`): block tags become newlines,
  entities are decoded, tags stripped. The excerpt's entities (`&amp;`,
  `&hellip;`) go through the same decoder.
- Fetch uses `Mozilla/5.0 (compatible; jobicy-cli/1.0)`,
  `Accept: application/json`, and exponential backoff with jitter on 429/5xx
  (max 6 attempts). A connection error fails fast with a clear message.
- `robots.txt` disallows site-search query paths (`/*?s=` etc.) — this skill
  does not crawl those paths; it uses the official public API, which is the
  sanctioned access method.
