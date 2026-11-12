# Working Nomads API reference

The endpoint and response shape this skill depends on. This is the file to
update if the feed changes. Base: `https://www.workingnomads.com`.

Verified against the live feed (2026-11-12).

## Authentication

None for the public feed below. The authenticated search API (`/api/jobs/`,
401 `Authentication credentials were not provided` without a token) is NOT
used by this skill — per the no-accounts rule it stays out of scope.

## `GET /api/exposed_jobs/`

The only endpoint the skill uses, for both `search` (whole feed, filtered
client-side) and `detail` (find by id within the feed). Public, paginated
server-side only by recency (37 jobs at verification time) — no search
parameter (verified: `?search=python` returns the identical full feed).

### Job object (the fields the skill reads)

```jsonc
{
  "url": "https://www.workingnomads.com/job/go/2016531/",  // -> result.url; id extracted from it
  "title": "Senior React Full-stack Developer",             // -> result.title
  "description": "<p>...</p>",                              // HTML; cleaned client-side by cleanHtml
  "company_name": "Lemon.io",                               // -> result.company
  "category_name": "Development",                          // --category matches this
  "location": "Europe, LATAM, APAC, the U.S., Canada",     // -> result.location
  "tags": "react,python,nodejs,ruby,english",              // one CSV string; split into a list
  "pub_date": "2026-11-12T06:47:52-05:00"                  // -> result.date (nullable)
}
```

## Parsing notes

- The response is JSON, so there is no HTML card parsing. The only markup
  handling is `cleanHtml` (`cli/src/helpers.ts`): block tags become newlines,
  entities are decoded, tags stripped.
- `tags` is a single comma-separated string upstream (empty string when none);
  `toDetail` splits it on commas into `tags[]`.
- Fetch uses `Mozilla/5.0 (compatible; working-nomads-cli/1.0)`,
  `Accept: application/json`, and exponential backoff with jitter on 429/5xx
  (max 6 attempts). A connection error fails fast with a clear message.
- `robots.txt` is `User-agent: * / Disallow:` (allow all).
