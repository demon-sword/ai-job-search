# jobs24x API reference

The endpoints, parameters, and cryptographic wrapping this skill depends on.
This is the file to update if jobs24x changes its edge functions. Base:
`https://nuzenytiidpypvixcjet.supabase.co`.

Verified against the live site (2026-11-12). All findings below were obtained
without an account, using only what the public site sends for anonymous
visitors.

## How the site loads its listing

`/jobs` and `/jobs/<uuid>` both return the same ~7 KB React shell (`<div
id="root">` + JS bundles) — there is no server-rendered content to scrape. The
bundle (`/assets/index-*.js`) calls two Supabase edge functions with the
site's **public** anon key (embedded in the bundle for every visitor; Supabase
anon keys are public by design and gated server-side by RLS/policies):

| Call | Headers | Response |
|------|---------|----------|
| `GET /functions/v1/session-key` | `apikey: <public anon key>` (+ `Authorization: Bearer <token>` when signed in) | `{v: 1, audience, key (base64 AES-GCM), ttl}` — works anonymously |
| `GET /functions/v1/list-jobs?<params>` | same | `{v: 1, iv, ct}` — AES-GCM envelope; decrypt with the session key, `additionalData` = audience |

The decrypted payload is `{_aud, _ts, data: {jobs, page, totalPages, total, isPaid}}`.
No other free endpoint exposes titles, companies, or URLs (the SEO sitemap
lists 5,000 `/jobs/<uuid>` URLs but carries no title/company metadata).

Verified anonymous query params (mirroring the bundle's filter state):
`page`, `query`, `remote`, `employment_type`, `country`, plus `date_posted`,
`experience`, `jobFunction`, `sort`, `minSalary`, `hiddenOnly`,
`lowCompetitionOnly`, `remoteUsd` (unverified vocabulary — the CLI passes
through only the four documented flags).

## The lock (server-side, not bypassable client-side)

An anonymous `list-jobs` call for `query=python` returned 95 matches, 12 per
page — every row:

```jsonc
{
  "id": "eb7ed263-0eb0-4c0f-a602-be06f92be",
  "title": "Software Engineer, Deployment Compliance",  // real title
  "description": "Minimum Qualifications. 2+ years ...", // truncated snippet
  "date_posted": "2026-11-11",
  "location": "Not specified", "remote_status": "On-site",
  "url": "#locked",                                     // no apply URL
  "companies": { "name": "Jobs", "logo_url": null },    // redacted; other rows carry
                                                        // title fragments ("Rad AI", ...)
  "locked": true, "ai_insights": null, "hidden_score": null,
  "salary_min_estimated": null, ...
}
```

`isPaid: false` at the payload level. The company name and apply URL are simply
absent from the anonymous payload — there is nothing client-side to decrypt or
scrape around it.

## Paid unlock (UNVERIFIED — inference, never tested)

A signed-in request adds `Authorization: Bearer <supabase access token>` to
both calls. The `isPaid` flag and the `locked`/`#locked` shape strongly suggest
a paid session returns `isPaid: true` with real `companies.name` and `url`
values — but no account was created and nothing was paid, so the CLI treats
`JOBS24X_API_TOKEN` as an opaque bearer passthrough and keeps emitting whatever
lock state the server returns. If paid rows come back unlocked, the CLI surfaces
them verbatim (real company + URL).

## Key rotation

If `session-key` starts rejecting the bundled anon key, extract the fresh one
from the current bundle: fetch `/jobs`, find `/assets/index-*.js`, and grep
for the `eyJ...` JWT next to `supabase.co`. Update `PUBLIC_ANON_KEY` in
`cli/src/helpers.ts`.

## Parsing notes

- The listing payload is JSON (post-decryption); nothing is scraped from HTML.
- Fetch uses `Mozilla/5.0 (compatible; jobs24x-cli/1.0)` and exponential
  backoff with jitter on 429/5xx (max 6 attempts). A connection error fails fast.
