#!/usr/bin/env python3
"""Autonomous scheduled sweep: discovery, dedupe, triage, tracker rows.

Discovery and ranking only. This module never sends data anywhere, never
messages anyone, never creates profiles, and never handles sign-in material. The
captain's per-role pick stays the gate before any later workflow step.

Pipeline (each stage is a pure function except the marked edges):

1. Discovery (edge: subprocess/network) - run the standing portal queries:
   freehire + LinkedIn CLIs plus the Cutshort/Wellfound static listing shapes
   from the prior sweep reports. Query shapes live in FREEHIRE_QUERIES,
   LINKEDIN_QUERIES, CUTSHORT_LISTINGS, WELLFOUND_LISTINGS below.
2. Dedupe - content-hash identity (job_identity), never title matching.
   A find is already seen when its metadata hash OR its full hash matches
   state, or when its company+role pair is already on the tracker.
3. Triage - deterministic keyword scorer reusing the standing weights from
   tools/rank_state.py (Technical 30 / Experience 25 / Behavioral 15 /
   Career 30; Location is a pass/fail gate, not weighted). Same input text
   always yields the same scores: no clock, no randomness, no network.
4. Notify through existing surfaces only - a dated shortlist artifact under
   reports/sweeps/ plus tracker rows (status ``drafted``: nothing sent) so
   /rank and /html-report pick the finds up with zero new channels.

Scheduler: ``sweep_auto.py due`` reports whether CADENCE_DAYS elapsed since
the last recorded run (state file holds one ISO date). Wire it to cron, e.g.
``0 9 * * 1 python3 tools/sweep_auto.py run`` from the repo root for a weekly
sweep; ``run`` records the date only after the artifact + tracker + state
writes all succeed.
"""

import argparse
import csv
import datetime as _dt
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from rank_state import WEIGHTS, band, norm as tracker_norm, overall_score, save_state
except ImportError:  # `python3 tools/sweep_auto.py` with cwd elsewhere
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from rank_state import WEIGHTS, band, norm as tracker_norm, overall_score, save_state

ROOT = Path(__file__).resolve().parent.parent
SEEN = ROOT / "job_scraper" / "seen_jobs.json"
TRACKER = ROOT / "job_search_tracker.csv"
SCHEDULER_STATE = ROOT / "job_scraper" / "sweep_auto_state.json"
ARTIFACT_DIR = ROOT / "reports" / "sweeps"

TRACKER_HEADER = (
    "date,company,sector,role,role_type,channel,status,"
    "contact_person,fit_rating,notes,cv_file,cover_letter_file,source,deadline"
)

CADENCE_DAYS = 14
SHORTLIST_FLOOR = 60  # Good Fit and up lands on the tracker; the rest is artifact-only
CANDIDATE_YOE = 3  # ~3 yrs production AI engineering per the candidate profile

# ---------------------------------------------------------------------------
# 1. Discovery queries (shapes lifted from the prior sweep reports)
# ---------------------------------------------------------------------------

# Each entry: (query text, jobage-days window). Run with
# --category ml_ai --remote remote --country IN --format json
FREEHIRE_QUERIES = [
    ("AI engineer", 14),
    ("Agentic AI engineer", 21),
    ("GenAI engineer", 14),
    ("LLM engineer", 14),
    ("Senior AI engineer", 14),
    ("Senior LLM engineer", 14),
    ("Applied AI engineer", 14),
    ("MLOps engineer", 14),
]

# Each entry: (query text, LinkedIn location, jobage-days window).
LINKEDIN_QUERIES = [
    ("AI engineer", "Delhi, India", 30),
    ("AI engineer", "Gurgaon, Haryana, India", 14),
    ("AI engineer", "Noida, Uttar Pradesh, India", 14),
    ("Senior AI engineer", "Delhi, India", 14),
    ("Generative AI engineer", "Noida, Uttar Pradesh, India", 30),
    ("Generative AI engineer", "Gurgaon, Haryana, India", 30),
]

# Static server-rendered listing pages (recency-capped at ~50 cards each).
CUTSHORT_LISTINGS = [
    "https://cutshort.io/jobs/remote-machine-learning-ml-jobs",
    "https://cutshort.io/jobs/artificial-intelligence-ai-jobs",
]
WELLFOUND_LISTINGS = [
    "https://wellfound.com/role/l/ai-engineer/india",
    "https://wellfound.com/role/l/machine-learning-engineer/india",
]

FREEHIRE_CLI = [".agents/skills/freehire-search/cli/src/cli.ts"]
LINKEDIN_CLI = [".agents/skills/linkedin-search/cli/src/cli.ts"]
_FETCH_TIMEOUT = 60
_CLI_TIMEOUT = 180
_UA = {"User-Agent": "Mozilla/5.0 (compatible; sweep-auto/1.0)"}


def freehire_commands():
    """Bun CLI invocations for every standing freehire query (data, not run)."""
    cmds = []
    for query, jobage in FREEHIRE_QUERIES:
        cmds.append(
            ["bun", "run", *FREEHIRE_CLI, "search", "-q", query,
             "--category", "ml_ai", "--remote", "remote", "--country", "IN",
             "--jobage", str(jobage), "--limit", "30",
             "--no-description", "--format", "json"]
        )
    return cmds


def linkedin_commands():
    """Bun CLI invocations for every standing LinkedIn query (data, not run)."""
    cmds = []
    for query, location, jobage in LINKEDIN_QUERIES:
        cmds.append(
            ["bun", "run", *LINKEDIN_CLI, "search", "-q", query,
             "-l", location, "--jobage", str(jobage), "--limit", "15",
             "--format", "json"]
        )
    return cmds


def _get(url):
    req = Request(url, headers=_UA)
    with urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


_HREF = re.compile(r'href="([^"]+)"')


def static_listing_links(listing_url):
    """Posting links found on one static listing page (read-only fetch)."""
    try:
        html = _get(listing_url)
    except Exception as exc:  # host down, markup moved: degrade, never abort
        print(f"sweep-auto: listing fetch failed for {listing_url}: {exc}", file=sys.stderr)
        return []
    links = []
    for href in _HREF.findall(html):
        if re.match(r"^/job/[A-Za-z0-9-]+$", href):
            links.append("https://cutshort.io" + href)
        elif re.match(r"^/jobs/\d+-[a-z0-9-]+$", href):
            links.append("https://wellfound.com" + href)
    return sorted(set(links))


def discover():
    """Run every discovery query; return raw posting dicts. Failures degrade."""
    postings = []
    for cmd in freehire_commands() + linkedin_commands():
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=_CLI_TIMEOUT, cwd=ROOT)
        except Exception as exc:
            print(f"sweep-auto: CLI failed ({' '.join(cmd[3:6])}): {exc}", file=sys.stderr)
            continue
        if out.returncode != 0:
            print(f"sweep-auto: CLI error ({' '.join(cmd[3:6])}): "
                  f"{out.stderr.strip()[:200]}", file=sys.stderr)
            continue
        try:
            payload = json.loads(out.stdout)
        except json.JSONDecodeError:
            continue
        results = payload.get("results", payload if isinstance(payload, list) else [])
        portal = "freehire-search" if "freehire" in cmd[2] else "linkedin-search"
        for row in results:
            if not isinstance(row, dict):
                continue
            postings.append({
                "title": row.get("title", ""),
                "company": row.get("company", ""),
                "location": row.get("location", ""),
                "url": row.get("url", ""),
                "description": row.get("description", ""),
                "portal": portal,
                "posted_date": row.get("date"),
            })
    for listing in CUTSHORT_LISTINGS + WELLFOUND_LISTINGS:
        portal = "cutshort" if "cutshort" in listing else "wellfound"
        for url in static_listing_links(listing):
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            postings.append({
                "title": re.sub(r"[-_]+", " ", re.sub(r"^[0-9]+-", "", slug)),
                "company": "",
                "location": "",
                "url": url,
                "description": "",
                "portal": portal,
                "posted_date": None,
            })
    return postings


# ---------------------------------------------------------------------------
# 2. Content-hash identity (never title matching)
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _norm(text):
    return _WS.sub(" ", str(text or "").lower()).strip()


def canonical_url(url):
    """Lowercase scheme+host, drop fragment/query, strip trailing slash."""
    url = str(url or "").strip()
    url = url.split("#", 1)[0].split("?", 1)[0]
    if "://" in url:
        scheme, rest = url.split("://", 1)
        host, _, path = rest.partition("/")
        url = scheme.lower() + "://" + host.lower() + "/" + path
    return url.rstrip("/")


def identity_meta(company, title, location="", url=""):
    """Hash of the posting's metadata (matches state entries that store no text)."""
    blob = "|".join([_norm(company), _norm(title), _norm(location), canonical_url(url)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def identity_full(company, title, location="", url="", description=""):
    """Hash of metadata plus posting text (distinct bodies stay distinct)."""
    blob = "|".join([_norm(company), _norm(title), _norm(location),
                     canonical_url(url), _norm(description)])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def seen_identities(doc):
    """Return (meta-set, full-set) of identities already in seen state."""
    meta, full = set(), set()
    for entry in (doc.get("seen") or {}).values():
        if not isinstance(entry, dict):
            continue
        meta.add(identity_meta(entry.get("company"), entry.get("title"),
                               entry.get("location"), entry.get("url")))
        stored = entry.get("identity")
        if isinstance(stored, str) and stored:
            full.add(stored)
    return meta, full


def is_seen(posting, meta_set, full_set):
    ident = identity_full(posting.get("company"), posting.get("title"),
                          posting.get("location"), posting.get("url"),
                          posting.get("description"))
    if ident in full_set:
        return True, ident
    meta_ident = identity_meta(posting.get("company"), posting.get("title"),
                               posting.get("location"), posting.get("url"))
    return (meta_ident in meta_set), ident


# ---------------------------------------------------------------------------
# 3. Deterministic triage scorer (standing weights from rank_state)
# ---------------------------------------------------------------------------

# Phrases grounded in the candidate profile (agentic/RAG/eval/MCP/production AI).
CORE_PHRASES = [
    "agent", "agentic", "multi-agent", "orchestration", "llm",
    "retrieval-augmented", "rag", "evals", "evaluation", "mcp",
    "prompt engineering", "langchain", "langgraph", "vector",
    "embedding", "guardrail", "genai", "generative ai",
]
STACK_PHRASES = [
    "python", "fastapi", "docker", "terraform", "qdrant",
    "claude code", "bedrock", "ci",
]
SENIOR_TITLES = ["lead", "principal", "staff", "architect", "director",
                 "manager", "head of", "founding"]
JUNIOR_MARKS = ["junior", "entry", "fresher", "intern"]
SERVICES_MARKS = ["consulting", "consultancy", "staffing", "agency",
                  "client delivery", "solutions provider"]
GRIND_MARKS = ["5.5-day", "5.5 day", "6-day", "6 day work", "pacific overlap",
               "night shift", "weekend work"]
OWNERSHIP_MARKS = ["ownership", "own the", "small team", "startup",
                   "0 to 1", "0-to-1", "end-to-end", "end to end"]
REMOTE_PASS = ["remote", "work from home", "work-from-home", "hybrid"]
NCR_CITIES = ["delhi", "noida", "gurgaon", "gurugram", "faridabad", "ghaziabad"]
RELOCATE_FAIL = ["relocation", "relocate", "onsite bengaluru", "onsite mumbai",
                 "onsite hyderabad", "onsite pune", "onsite chennai",
                 "bangalore onsite", "work from office"]
_YOE = re.compile(r"(\d+)\s*(?:\+)?\s*(?:-\s*(\d+))?\s*(?:yrs|years|yoe)\b")


def _count(text, phrases):
    return sum(1 for phrase in phrases if phrase in text)


def score_technical(text):
    core = _count(text, CORE_PHRASES)
    stack = _count(text, STACK_PHRASES)
    return max(5, min(98, 15 + 9 * min(core, 7) + 4 * min(stack, 5)))


def score_experience(text, title):
    title_lc = _norm(title)
    if any(mark in title_lc for mark in SENIOR_TITLES):
        return 35
    lo = None
    for match in _YOE.finditer(text):
        lo = int(match.group(1))
        break
    if lo is None:
        if any(mark in text for mark in JUNIOR_MARKS):
            return 65
        return 60
    if lo <= CANDIDATE_YOE:
        return 85
    if lo <= 5:
        return 60
    return 35


def score_behavioral(text):
    score = 60
    if any(mark in text for mark in OWNERSHIP_MARKS):
        score += 8
    if any(mark in text for mark in SERVICES_MARKS):
        score -= 10
    if any(mark in text for mark in GRIND_MARKS):
        score -= 8
    return max(5, min(95, score))


def score_career(text, title):
    if any(mark in _norm(title) for mark in SENIOR_TITLES):
        return 40
    core = _count(text, CORE_PHRASES)
    if core >= 4:
        return 85
    if core >= 2:
        return 70
    if core >= 1:
        return 55
    return 35


def location_verdict(location, text):
    blob = _norm(location) + " " + text
    if any(mark in blob for mark in RELOCATE_FAIL):
        return "FAIL", "requires relocation or onsite outside NCR"
    if "india" in blob and ("remote" in blob or "hybrid" in blob):
        return "PASS", "remote/hybrid India"
    if any(city in _norm(location) for city in NCR_CITIES):
        return "PASS", "Delhi NCR"
    if any(mark in blob for mark in REMOTE_PASS):
        return "FLAG", "remote claimed, India seat unconfirmed"
    return "FLAG", "work mode unclear from posting text"


def triage(posting):
    """Score one posting dict. Pure function: same text, same result."""
    assert set(WEIGHTS) == {"technical", "experience", "behavioral", "career"}
    text = _norm(f"{posting.get('title', '')} {posting.get('description', '')}")
    scores = {
        "technical": score_technical(text),
        "experience": score_experience(text, posting.get("title", "")),
        "behavioral": score_behavioral(text),
        "career": score_career(text, posting.get("title", "")),
    }
    verdict, note = location_verdict(posting.get("location", ""), text)
    strengths = [p for p in CORE_PHRASES if p in text][:3]
    gaps = []
    if scores["experience"] <= 40:
        gaps.append("yoe bar above ~3 yrs or senior-tilted title")
    if any(mark in text for mark in SERVICES_MARKS):
        gaps.append("services/consulting tilt")
    if any(mark in text for mark in GRIND_MARKS):
        gaps.append("schedule/overlap strain")
    if not strengths:
        gaps.append("weak agentic/RAG/eval signal")
    return {
        "scores": scores,
        "overall": overall_score(scores),
        "verdict": band(overall_score(scores)),
        "location_verdict": verdict,
        "location_note": note,
        "strengths": strengths,
        "gaps": gaps[:3],
    }


# ---------------------------------------------------------------------------
# 4. Run: dedupe -> triage -> artifact + tracker rows + state
# ---------------------------------------------------------------------------

def _today():
    return _dt.date.today().isoformat()


def run_sweep(postings, seen_doc, tracker_rows, run_date=None):
    """Dedupe + triage one batch. Returns (results, new_seen_entries, new_tracker_rows)."""
    run_date = run_date or _today()
    meta_set, full_set = seen_identities(seen_doc)
    tracked = set()
    for row in tracker_rows:
        tracked.add((tracker_norm(row.get("company", "")), tracker_norm(row.get("role", ""))))
    results, fresh = [], []
    for posting in postings:
        seen, ident = is_seen(posting, meta_set, full_set)
        if seen:
            continue
        if (tracker_norm(posting.get("company")), tracker_norm(posting.get("title"))) in tracked:
            continue
        # Reserve both hashes so a repost with altered body still dedupes.
        meta_set.add(identity_meta(posting.get("company"), posting.get("title"),
                                   posting.get("location"), posting.get("url")))
        full_set.add(ident)
        scored = triage(posting)
        scored.update({
            "title": posting.get("title", ""),
            "company": posting.get("company", ""),
            "location": posting.get("location", ""),
            "url": posting.get("url", ""),
            "portal": posting.get("portal", ""),
            "posted_date": posting.get("posted_date"),
            "identity": ident,
        })
        results.append(scored)
        fresh.append(scored)
    results.sort(key=lambda r: r["overall"], reverse=True)

    shortlist = [r for r in results
                 if r["overall"] >= SHORTLIST_FLOOR and r["location_verdict"] != "FAIL"]
    new_tracker_rows = []
    for r in shortlist:
        note_bits = [f"sweep-auto {run_date} triage {r['overall']}/{r['verdict']}"]
        if r["strengths"]:
            note_bits.append("signals: " + ", ".join(r["strengths"]))
        if r["gaps"]:
            note_bits.append("gaps: " + "; ".join(r["gaps"]))
        if r["location_verdict"] == "FLAG":
            note_bits.append("location: " + r["location_note"])
        new_tracker_rows.append({
            "date": run_date,
            "company": r["company"],
            "sector": "",
            "role": r["title"],
            "role_type": "",
            "channel": "sweep",
            "status": "drafted",
            "contact_person": "",
            "fit_rating": r["verdict"],
            "notes": ". ".join(note_bits),
            "cv_file": "",
            "cover_letter_file": "",
            "source": r["url"],
            "deadline": "",
        })
    new_seen_entries = {}
    for r in fresh:
        new_seen_entries[r["identity"]] = {
            "title": r["title"],
            "company": r["company"],
            "url": r["url"],
            "location": r.get("location", ""),
            "first_seen": run_date,
            "posted_date": r.get("posted_date"),
            "deadline": None,
            "fit": ("high" if r["overall"] >= 75
                    else "medium" if r["overall"] >= 60 else "low"),
            "status": "new",
            "portal": r.get("portal", ""),
            "source": "sweep-auto",
            "identity": r["identity"],
            "rank_score": r["overall"],
        }
    return results, new_seen_entries, new_tracker_rows


def write_artifact(results, run_date, artifact_dir=None):
    """Write the dated ranked shortlist; returns the artifact path."""
    artifact_dir = Path(artifact_dir) if artifact_dir else ARTIFACT_DIR
    artifact_dir.mkdir(parents=True, exist_ok=True)
    shortlist = [r for r in results
                 if r["overall"] >= SHORTLIST_FLOOR and r["location_verdict"] != "FAIL"]
    rest = [r for r in results if r not in shortlist]
    lines = [f"# Sweep shortlist - {run_date}", "",
             f"Found {len(results)} new postings "
             f"({len(shortlist)} shortlisted, {len(rest)} below bar).",
             "Triage scores from posting text only; the captain's per-role pick "
             "gates any later workflow step.", "",
             "## Shortlist", ""]
    for i, r in enumerate(shortlist, 1):
        flag = " FLAG " + r["location_note"] if r["location_verdict"] == "FLAG" else ""
        lines.append(f"{i}. **{r['title']}** - {r['company']} "
                     f"({r['overall']}/{r['verdict']}){flag}")
        lines.append(f"   {r['url']} [{r['portal']}]")
    lines += ["", "## Below bar", ""]
    for r in rest:
        lines.append(f"- {r['title']} - {r['company']} "
                     f"({r['overall']}/{r['verdict']}, location {r['location_verdict']})")
    path = artifact_dir / f"sweep-{run_date}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def append_tracker_rows(rows, tracker_path=None):
    """Append rows to the tracker, creating it with the standard header."""
    tracker_path = Path(tracker_path) if tracker_path else TRACKER
    exists = tracker_path.exists()
    cols = TRACKER_HEADER.split(",")
    with open(tracker_path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return tracker_path


def read_tracker_rows(tracker_path=None):
    tracker_path = Path(tracker_path) if tracker_path else TRACKER
    if not tracker_path.exists():
        return []
    with open(tracker_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def last_run(state_path=None):
    state_path = Path(state_path) if state_path else SCHEDULER_STATE
    if not state_path.exists():
        return None
    try:
        value = json.loads(state_path.read_text(encoding="utf-8")).get("last_run")
    except (json.JSONDecodeError, AttributeError):
        return None
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", value):
        return value
    return None


def is_due(cadence_days=CADENCE_DAYS, today=None, state_path=None):
    """True when no run is recorded or the cadence window has elapsed."""
    today = today or _today()
    previous = last_run(state_path)
    if previous is None:
        return True
    return previous <= (str((_dt.date.fromisoformat(today)
                             - _dt.timedelta(days=cadence_days))))


def record_run(run_date, state_path=None):
    state_path = Path(state_path) if state_path else SCHEDULER_STATE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"last_run": run_date}, indent=2) + "\n",
                          encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_due(args):
    due = is_due(args.cadence_days, state_path=args.state)
    print("due" if due else f"not due (last run {last_run(args.state)})")
    return 0


def cmd_queries(_args):
    print(f"# freehire ({len(FREEHIRE_QUERIES)}): --category ml_ai --remote remote --country IN")
    for query, jobage in FREEHIRE_QUERIES:
        print(f"  -q {query!r} --jobage {jobage}")
    print(f"# linkedin ({len(LINKEDIN_QUERIES)})")
    for query, location, jobage in LINKEDIN_QUERIES:
        print(f"  -q {query!r} -l {location!r} --jobage {jobage}")
    print(f"# cutshort static ({len(CUTSHORT_LISTINGS)}):")
    for url in CUTSHORT_LISTINGS:
        print(f"  {url}")
    print(f"# wellfound static ({len(WELLFOUND_LISTINGS)}):")
    for url in WELLFOUND_LISTINGS:
        print(f"  {url}")
    return 0


def cmd_run(args):
    run_date = args.date or _today()
    postings = discover()
    seen_path = Path(args.seen)
    if seen_path.exists():
        seen_doc = json.loads(seen_path.read_text(encoding="utf-8"))
    else:
        seen_doc = {"seen": {}}
    tracker_rows = read_tracker_rows(args.tracker)
    results, new_entries, new_tracker = run_sweep(postings, seen_doc, tracker_rows,
                                                  run_date=run_date)
    artifact = write_artifact(results, run_date, args.artifacts)
    if new_tracker:
        append_tracker_rows(new_tracker, args.tracker)
    seen_doc.setdefault("seen", {}).update(new_entries)
    save_state(seen_path, seen_doc)
    record_run(run_date, args.state)
    print(f"sweep {run_date}: {len(postings)} discovered, "
          f"{len(results)} new, {len(new_tracker)} shortlisted -> {artifact}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seen", default=str(SEEN))
    ap.add_argument("--tracker", default=str(TRACKER))
    ap.add_argument("--state", default=str(SCHEDULER_STATE))
    ap.add_argument("--artifacts", default=str(ARTIFACT_DIR))
    sub = ap.add_subparsers(dest="cmd", required=True)

    due_p = sub.add_parser("due", help="report whether the cadence window elapsed")
    due_p.add_argument("--cadence-days", type=int, default=CADENCE_DAYS)
    due_p.set_defaults(func=cmd_due)

    q_p = sub.add_parser("queries", help="print the standing discovery queries")
    q_p.set_defaults(func=cmd_queries)

    run_p = sub.add_parser("run", help="discover, dedupe, triage, write outputs")
    run_p.add_argument("--date", default=None, help="run date YYYY-MM-DD (default today)")
    run_p.add_argument("--cadence-days", type=int, default=CADENCE_DAYS)
    run_p.set_defaults(func=cmd_run)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
