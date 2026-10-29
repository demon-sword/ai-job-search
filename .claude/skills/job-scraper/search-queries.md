# Search Queries for Job Scraper

<!-- SETUP: Customize these queries based on your skills, target roles, and location -->

## Installed portal CLIs (primary for `/scrape`)

`/scrape` discovers every portal skill under `.agents/skills/*/SKILL.md` and runs its CLI first. Shipped country-agnostic CLIs include `linkedin-search` and `freehire-search`; Danish demos and any skill you add with `/add-portal` are included the same way. You do **not** need a matching `site:` line below for those CLIs to run.

The `site:` query templates in this file are the **WebSearch fallback** -- for portals without a CLI, company career pages, or when a CLI fails.

**Language scope:** write every query category in every language listed in your CLAUDE.md Languages table (typically 1-2, sometimes more). A posting requiring a language you have *not* declared, as a job condition, is excluded before scoring; a posting requiring a *higher level* than you declared in a language you *do* work in is flagged for your own judgment, not excluded -- see `04-job-evaluation.md`'s Language Gate, the single source of truth for this rule. Translate each category's keywords rather than machine-translating word-for-word (e.g. "Frontend Developer" -> "Desarrollador Frontend", not a literal word-for-word translation) if you work in more than one language.

## Search Sites

Primary (your market's job boards - scaffold one with `/add-portal`):
- **LinkedIn Jobs** - covered by `linkedin-search` CLI (location: "Remote" or "Noida, Uttar Pradesh, India" / "Delhi, India")
- **freehire.me** - covered by `freehire-search` CLI (tech-focused, remote-first, multi-market via --region/--country)
- **Wellfound (AngelList)** - startup/tech jobs (add via `/add-portal` if needed)
- **We Work Remotely** - remote jobs (add via `/add-portal` if needed)
- **Remote OK** - remote jobs (add via `/add-portal` if needed)
- **Naukri.com** - India's largest job board (add via `/add-portal` if needed)

Secondary (company career pages via Google):
- Direct Google searches with `site:` filters for known target companies

## Query Categories

Queries are grouped by priority. Write **each category in every language from your Languages table** (see Language scope above). Combine each query with your location terms (e.g. your city, region, or metro area) where the site supports it.

**Organize by function, not job title.** The same underlying work carries different titles across companies and markets (a "Data Scientist" role at one employer may be posted as "Insights Analyst" or "Data Consultant" at another). Name each priority category after the function it covers, and list several plausible job titles as query variants within that category rather than betting an entire priority tier on one exact title string.

### Priority 1: AI Engineer / GenAI Engineer / Agent Engineer

These match your strongest and most desired career direction.

```
# linkedin-search CLI (primary)
-q "AI Engineer" -l "Remote" --remote remote --jobage 14
-q "GenAI Engineer" -l "Remote" --remote remote --jobage 14
-q "Agent Engineer" -l "Remote" --remote remote --jobage 14
-q "LLM Engineer" -l "Remote" --remote remote --jobage 14
-q "AI Engineer" -l "Noida, Uttar Pradesh, India" --remote hybrid --jobage 14
-q "GenAI Engineer" -l "Delhi, India" --remote hybrid --jobage 14

# freehire-search CLI (primary)
-q "AI engineer" --remote remote --region global --jobage 14
-q "genai engineer" --remote remote --region global --jobage 14
-q "agent engineer" --remote remote --region global --jobage 14
-q "llm engineer" --remote remote --region global --jobage 14
-q "ai engineer" --country IN --remote remote --jobage 14
-q "genai engineer" --country IN --remote hybrid --jobage 14

# WebSearch fallback
site:linkedin.com/jobs "AI Engineer" "Remote"
site:linkedin.com/jobs "GenAI Engineer" "Remote"
site:linkedin.com/jobs "Agent Engineer" "Remote"
site:linkedin.com/jobs "LLM Engineer" "Remote"
site:freehire.me "AI engineer" remote
site:wellfound.com "AI Engineer" remote
site:weworkremotely.com "AI Engineer"
```

### Priority 2: ML Engineer / MLOps / AI Platform

These match your domain expertise in ML engineering, evaluation, and production AI.

```
# linkedin-search CLI
-q "ML Engineer" -l "Remote" --remote remote --jobage 14
-q "MLOps Engineer" -l "Remote" --remote remote --jobage 14
-q "AI Platform Engineer" -l "Remote" --remote remote --jobage 14
-q "Machine Learning Engineer" -l "Remote" --remote remote --jobage 14
-q "ML Engineer" -l "Noida, Uttar Pradesh, India" --remote hybrid --jobage 14

# freehire-search CLI
-q "ml engineer" --remote remote --region global --jobage 14
-q "mlops engineer" --remote remote --region global --jobage 14
-q "ml platform engineer" --remote remote --region global --jobage 14
-q "machine learning engineer" --country IN --remote remote --jobage 14

# WebSearch fallback
site:linkedin.com/jobs "ML Engineer" "Remote"
site:linkedin.com/jobs "MLOps Engineer" "Remote"
site:linkedin.com/jobs "AI Platform Engineer" "Remote"
site:freehire.me "ml engineer" remote
```

### Priority 3: Senior Software Engineer (AI/ML focus)

Adjacent roles you could pivot into -- strong backend/engineering with AI focus.

```
# linkedin-search CLI
-q "Senior Software Engineer AI" -l "Remote" --remote remote --jobage 14
-q "Senior Software Engineer ML" -l "Remote" --remote remote --jobage 14
-q "Backend Engineer AI" -l "Remote" --remote remote --jobage 14
-q "Senior Software Engineer" -l "Remote" --remote remote --jobage 14 -q "Python" -q "AI"

# freehire-search CLI
-q "senior software engineer" --category backend --skill python --remote remote --jobage 14
-q "backend engineer" --skill python --skill ai --remote remote --jobage 14

# WebSearch fallback
site:linkedin.com/jobs "Senior Software Engineer" "AI" "Remote"
site:linkedin.com/jobs "Senior Software Engineer" "Machine Learning" "Remote"
```

### Priority 4: Broader Technical / Applied AI

Wider net for general technical roles with AI component.

```
# linkedin-search CLI
-q "Applied Scientist" -l "Remote" --remote remote --jobage 14
-q "AI Research Engineer" -l "Remote" --remote remote --jobage 14
-q "Generative AI Engineer" -l "Remote" --remote remote --jobage 14
-q "RAG Engineer" -l "Remote" --remote remote --jobage 14

# freehire-search CLI
-q "applied scientist" --remote remote --region global --jobage 14
-q "generative ai" --remote remote --region global --jobage 14
-q "rag engineer" --remote remote --region global --jobage 14

# WebSearch fallback
site:linkedin.com/jobs "Applied Scientist" "Remote"
site:linkedin.com/jobs "Generative AI" "Remote"
site:linkedin.com/jobs "RAG Engineer" "Remote"
```

### Priority 5: Key Skills as Search Terms (supplementary)

Use these as additional filters or for portals with limited title search.

```
# linkedin-search CLI
-q "Google ADK" -l "Remote" --remote remote --jobage 14
-q "MCP" -l "Remote" --remote remote --jobage 14
-q "multi-agent" -l "Remote" --remote remote --jobage 14
-q "Promptfoo" -l "Remote" --remote remote --jobage 14
-q "ClickHouse" -l "Remote" --remote remote --jobage 14

# freehire-search CLI (facet filters)
--skill google-adk --remote remote --jobage 14
--skill mcp --remote remote --jobage 14
--skill multi-agent --remote remote --jobage 14
--skill promptfoo --remote remote --jobage 14
--skill clickhouse --remote remote --jobage 14
```

## Location Filter

When evaluating results, verify the job location is within reasonable commute distance from your home. Define acceptable areas:

- **Ideal:** Remote (fully remote, work from anywhere in India)
- **Acceptable:** Noida, Delhi (Delhi NCR - can live near family)
- **Borderline:** Gurgaon, Faridabad, Ghaziabad (NCR - longer commute, consider if remote/hybrid)
- **Too Far:** Mumbai, Bangalore, Hyderabad, Pune, Chennai, Kolkata, Ahmedabad, etc. (requires relocation - deal-breaker)

## Language Filter

Your working languages and levels are in CLAUDE.md's Languages table:
- English: Fluent
- Hindi: Native

When filtering scraped results, apply `04-job-evaluation.md`'s Language Gate: a posting requiring a language you haven't declared at all is excluded; a posting requiring a higher level than you declared in a language you do work in is not excluded, flag it clearly instead (see `job-scraper/SKILL.md`'s Step 3 "Quick Fit Assessment" for how the flag surfaces in `/scrape` output). Postings simply *written* in a language you don't work in, that don't require it on the job, are fine.

## Date Filter

Only include jobs posted within the last 14 days, or with an application deadline that has not yet passed. If a posting date cannot be determined, include it but flag as "date unknown".

## Adapting Queries

If the user specifies a focus area, select queries from the matching category and also generate 2-3 custom queries for that focus. For example:
- "/scrape [focus_area]" -> relevant category queries + custom focus-specific queries