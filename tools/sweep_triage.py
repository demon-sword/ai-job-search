#!/usr/bin/env python3
"""Cheap keyword triage over a sweep of postings: which ones deserve /rank.

/scrape collects stubs (title-only from the HTML-portal CLIs, short previews
at best) and /rank scores full posting text with agents. Scoring stubs against
a bar built for full descriptions shortlists nothing: a 15-posting sweep
scored 0 because title keywords cannot clear a full-text bar. This tool makes
the scores honest in three layers:

1. FULL TEXT FIRST - before scoring, fetch each posting's full description
   with its own portal's `detail` command (bounded per-portal fetch, cached
   locally, polite pacing; a portal failure degrades that posting to a stub,
   never fails the sweep). Postings whose search output already carries the
   full description (freehire-search hydrates by default) are scored as-is.
   Every row records which text was scored (see `path` / `text_source`).

2. STUB FALLBACK - when no full text exists, the stub is scored on its own
   scale against a separately calibrated stub floor with clearly-labeled
   `Stub:` bands. A thin snippet can therefore never masquerade as a scored
   miss, nor inflate into a false shortlist: stub rows are never shortlisted.

3. VOCABULARY - the match vocabulary covers the adjacent production-AI/ML
   signals around the agentic/RAG/eval niche (see the tier rationale below),
   so good AI jobs are not scored as irrelevant for missing niche keywords.

The 60 shortlist bar is shared with /rank's Good Fit floor and moves only
with stated evidence (before/after score distributions on the fixed fixture
set in tests/fixtures/sweep_triage/). It is not a tuning knob.

Usage:
  python3 tools/sweep_triage.py score --input postings.json [--no-fetch]
      [--max-fetch-per-portal N] [--pace SECONDS] [--cache-dir DIR]
      [--top N] [--floor N] [--limit N] [--out report.json]
  python3 tools/sweep_triage.py score --state job_scraper/seen_jobs.json ...

Input postings are JSON objects with key/title/company/url/portal and
optional snippet (search-preview text), detail_id (the portal-native lookup
for its `detail` command - freehire slug, jobnet jobAdId - falling back to
the URL when absent) and full_text (already-known JD text).
--state mode selects status=new entries from seen_jobs.json instead.

Prints the report JSON on stdout. Exit 0 on a completed sweep - zero
shortlisted is a valid outcome, not an error. Exit 2 on usage/state errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

try:
    from rank_state import BANDS as RANK_BANDS
    from rank_state import band as rank_band
except ImportError:  # pragma: no cover - rank_state always ships beside this tool
    RANK_BANDS = ((75, "Strong Fit"), (60, "Good Fit"), (45, "Moderate Fit"), (30, "Weak Fit"), (0, "Poor Fit"))

    def rank_band(score: int) -> str:
        for floor, name in RANK_BANDS:
            if score >= floor:
                return name
        return "Poor Fit"


ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# Vocabulary tiers and weight rationale.
#
# Evidence: the candidate profile
# (.claude/skills/job-application-assistant/01-candidate-profile.md) declares
# GenAI/agentic engineering as the core craft - "Multi-agent orchestration &
# LLM orchestration", "Agentic AI systems & RAG architectures", "MLOps & model
# evaluation (golden datasets, Promptfoo)" - with MLOps/PyTorch/FastAPI/data
# stores as adjacent production skills and Python/SQL/Docker/Terraform/CI-CD
# as the general engineering base. The scraper's search-queries.md organizes
# the same way by function: P1 AI/GenAI/Agent/LLM Engineer (strongest), P2
# ML/MLOps/AI Platform (domain expertise), P3 senior SWE with AI focus
# (adjacent pivot), P4 applied AI, P5 tool skills (ADK/MCP/multi-agent).
#
# Weights follow that order: niche terms that only a matching role would
# contain score highest; adjacent production-AI/ML terms score half; general
# engineering terms score a third, so a plain backend posting cannot outscore
# a genuine AI posting on stack words alone.
# --------------------------------------------------------------------------
CORE_PHRASES = (
    "ai engineer", "agent engineer", "llm engineer", "genai engineer", "rag engineer",
    "generative ai", "genai", "artificial intelligence",
    "ai agent", "ai agents", "agent", "agents", "agentic", "multi agent", "autonomous agent",
    "llm", "llms", "large language model", "foundation model",
    "rag", "retrieval augmented",
    "prompt engineering", "prompt engineer", "promptfoo", "evals", "model evaluation",
    "golden dataset", "llm evaluation",
    "langchain", "langgraph", "llamaindex",
    "vector database", "embeddings", "semantic search", "fine tuning", "fine tuned",
    "mcp", "model context protocol", "google adk",
    "guardrails", "hallucination",
)
ADJACENT_PHRASES = (
    "ml engineer", "machine learning engineer", "machine learning", "mlops",
    "applied scientist", "data scientist", "ml platform", "ai platform",
    "model serving", "model deployment", "inference", "real time inference",
    "feature store", "model monitoring", "model registry", "training pipeline",
    "data pipeline", "mlflow", "kubeflow", "sagemaker", "vertex ai",
    "pytorch", "tensorflow", "scikit learn", "sklearn", "xgboost",
    "nlp", "natural language", "computer vision", "recommender", "recommendation system",
    "personalization", "ranking model", "forecasting", "time series", "anomaly detection",
    "experimentation", "a b testing", "causal inference",
    "spark", "airflow", "kafka", "data engineering", "etl", "data warehouse", "clickhouse",
    "ml", "ai",
)
GENERAL_PHRASES = (
    "python", "fastapi", "sql", "docker", "kubernetes", "terraform", "ci cd", "jenkins",
    "aws", "gcp", "azure", "ec2", "rest api", "microservices", "backend", "typescript",
    "react", "node js", "java", "spring boot", "golang", "linux", "git", "cloud",
    "distributed systems", "system design",
)
TIERS = (("core", 3, CORE_PHRASES), ("adjacent", 2, ADJACENT_PHRASES), ("general", 1, GENERAL_PHRASES))

# Calibrated on the fixed fixture set tests/fixtures/sweep_triage/postings.json
# (15 real postings, 2026-11): saturation 30 puts the weakest genuine-AI full
# text at 60 and the strongest off-topic full text at 7, with adjacent-ML
# postings spread across the Moderate band - see the probe notes in the test.
FULL_SATURATION = 30
STUB_SATURATION = 10
# The shortlist bar. Pinned to /rank's Good Fit floor (rank_state.BANDS); the
# test asserts they stay equal, so moving one without the other fails loudly.
SHORTLIST_FLOOR = 60
# Stub bands prioritize fetch order only - a stub is never shortlisted no
# matter its score. Calibrated so a title-only exact-role stub ("AI Engineer"
# scores 50, "Machine Learning Engineer" scores 40) lands in the fetch bands
# while an off-topic title ("Registered Nurse" scores 0) does not.
STUB_STRONG_FLOOR = 45
STUB_WEAK_FLOOR = 20

DEFAULT_TOP = 5
DEFAULT_MAX_FETCH_PER_PORTAL = 10
DEFAULT_PACE = 1.5
FETCH_TIMEOUT = 90


def normalize(text: object) -> str:
    """Lowercase, non-alphanumeric to spaces, collapsed - phrases are stored
    in this same normalized form, so matching is exact-token, never substring
    ('ai' matches the token AI, never 'said' or 'OpenAI')."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]", " ", str(text or "").lower())).strip()


def matched_phrases(text: str, phrases: tuple[str, ...]) -> list[str]:
    padded = " " + normalize(text) + " "
    return sorted({p for p in phrases if (" " + p + " ") in padded})


def weighted_points(text: str) -> tuple[int, dict[str, list[str]]]:
    """Total matched weight plus the per-tier phrase lists behind it."""
    total = 0
    signals: dict[str, list[str]] = {}
    for name, weight, phrases in TIERS:
        found = matched_phrases(text, phrases)
        signals[name] = found
        total += weight * len(found)
    return total, signals


def score_on_scale(points: int, saturation: int) -> int:
    return min(100, round(100 * points / saturation))


def stub_text(posting: dict) -> str:
    snippet = (posting.get("snippet") or "").strip()
    title = (posting.get("title") or "").strip()
    return title + ("\n" + snippet if snippet else "")


def stub_band(score: int) -> str:
    if score >= STUB_STRONG_FLOOR:
        return "Stub: strong signal - fetch full text"
    if score >= STUB_WEAK_FLOOR:
        return "Stub: weak signal - fetch if budget allows"
    return "Stub: no signal"


# --------------------------------------------------------------------------
# Layer 1: full text first.
#
# Each portal CLI documents `detail <id|url> --format json` returning an
# object with a `description` field. Fetches are bounded (per-portal attempt
# cap plus a circuit breaker that stops calling a portal after two
# consecutive failures), cached by URL hash so re-runs cost nothing, and
# paced so a sweep never hammers a portal. Any failure degrades that posting
# to its stub - a portal outage must never fail the sweep.
# --------------------------------------------------------------------------
_PORTAL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_last_fetch_at = 0.0


def detail_target(posting: dict) -> str:
    """Portal-native lookup first (freehire slug, jobnet jobAdId, ...), the
    posting URL second. Employer/applicant-tracking URLs usually resolve
    nowhere in a portal CLI - passing them as the lookup is what makes a
    retrievable posting look expired, so the native id wins when present."""
    return posting.get("detail_id") or posting.get("url") or ""


def detail_command(portal: str, target: str) -> Optional[list[str]]:
    """The portal's detail invocation, or None when the portal cannot be
    trusted to run (unknown name, missing CLI)."""
    if not portal or not _PORTAL_NAME.match(portal):
        return None
    cli = ROOT / ".agents" / "skills" / portal / "cli" / "src" / "cli.ts"
    if not cli.is_file() or not target:
        return None
    return ["bun", "run", str(cli), "detail", target, "--format", "json"]


def run_detail(cmd: list[str]) -> Optional[str]:
    """Run one detail fetch; return the full description or None."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FETCH_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    for container in (payload, payload.get("result"), payload.get("job")):
        if isinstance(container, dict):
            desc = container.get("description")
            if isinstance(desc, str) and desc.strip():
                return desc
    return None


def cache_path(cache_dir: Path, url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return cache_dir / f"{digest}.json"


def fetch_full_text(
    posting: dict,
    cache_dir: Path,
    pace: float,
    attempts: dict[str, int],
    failures: dict[str, int],
    max_per_portal: int,
) -> tuple[Optional[str], str, Optional[str]]:
    """Return (description|None, via, error|None). `via` is detail_fetch,
    detail_cache, or a stub reason; callers fall back to the stub whenever
    the description is None."""
    global _last_fetch_at
    portal = posting.get("portal") or ""
    url = posting.get("url") or ""
    target = detail_target(posting)
    if failures.get(portal, 0) >= 2:
        return None, "stub", f"portal {portal or '?'} stopped after repeated failures"
    if attempts.get(portal, 0) >= max_per_portal:
        return None, "stub", f"portal {portal or '?'} hit the per-portal fetch cap"
    cmd = detail_command(portal, target)
    if cmd is None:
        return None, "stub", f"no detail command for portal {portal or '?'}"
    attempts[portal] = attempts.get(portal, 0) + 1
    cached = cache_path(cache_dir, target)
    try:
        entry = json.loads(cached.read_text(encoding="utf-8"))
        if isinstance(entry, dict) and entry.get("target") == target and entry.get("description"):
            return entry["description"], "detail_cache", None
    except (OSError, json.JSONDecodeError):
        pass
    wait = pace - (time.monotonic() - _last_fetch_at)
    if wait > 0:
        time.sleep(wait)
    desc = run_detail(cmd)
    _last_fetch_at = time.monotonic()
    if desc is None:
        failures[portal] = failures.get(portal, 0) + 1
        return None, "stub", "detail fetch returned no description"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(
            json.dumps({"target": target, "url": url, "description": desc}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass  # a cache write failure must not fail the sweep either
    return desc, "detail_fetch", None


def score_posting(
    posting: dict,
    fetch: bool,
    cache_dir: Path,
    pace: float,
    attempts: dict[str, int],
    failures: dict[str, int],
    max_per_portal: int,
) -> dict:
    """Score one posting. Chooses full-text vs stub, never both: pre-hydrated
    search text and fetched detail score on the full-text scale; anything
    else scores on the stub scale and is labeled as such."""
    known = (posting.get("full_text") or "").strip()
    fetch_note = None
    if known:
        text, via = known, "search_hydrated"
    elif fetch:
        text, via, fetch_note = fetch_full_text(posting, cache_dir, pace, attempts, failures, max_per_portal)
    else:
        text, via = None, "stub"
    if text:
        points, signals = weighted_points(posting.get("title", "") + "\n" + text)
        score = score_on_scale(points, FULL_SATURATION)
        path, band, shortlisted = "full_text", rank_band(score), score >= SHORTLIST_FLOOR
    else:
        points, signals = weighted_points(stub_text(posting))
        score = score_on_scale(points, STUB_SATURATION)
        # Structural, not numeric: a stub is never shortlisted no matter its
        # score, so a thin snippet can neither masquerade as a scored miss
        # (it carries a Stub: band, not a fit band) nor inflate into a false
        # shortlist entry.
        path, band, shortlisted = "stub", stub_band(score), False
    row = {
        "key": posting.get("key"),
        "title": posting.get("title"),
        "company": posting.get("company"),
        "url": posting.get("url"),
        "portal": posting.get("portal"),
        "path": path,
        "text_source": via if text else "stub",
        "score": score,
        "band": band,
        "shortlisted": shortlisted,
        "signals": signals,
    }
    if not text and fetch_note:
        row["fetch_note"] = fetch_note
    return row


def load_input_posts(args) -> list[dict]:
    if args.input:
        try:
            posts = json.loads(Path(args.input).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            sys.exit(f"cannot read input file {args.input}: {exc}")
        if isinstance(posts, dict):
            posts = posts.get("postings", posts.get("results", []))
        if not isinstance(posts, list):
            sys.exit(f"{args.input}: expected a JSON array of postings")
        return posts
    state_path = Path(args.state)
    if not state_path.is_file():
        sys.exit(f"{state_path} not found - run /scrape first")
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{state_path} is not valid JSON: {exc}")
    seen = doc.get("seen") if isinstance(doc, dict) and "seen" in doc else doc
    if not isinstance(seen, dict):
        sys.exit(f"{state_path}: expected an object of job entries")
    posts = [
        {
            "key": key,
            "title": entry.get("title"),
            "company": entry.get("company"),
            "url": entry.get("url"),
            "portal": entry.get("portal"),
            "snippet": "",
            "full_text": "",
        }
        for key, entry in seen.items()
        if entry.get("status") == "new"
    ]
    return posts


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["score"])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--input", help="JSON array of postings to triage")
    src.add_argument("--state", help="seen_jobs.json path (scores status=new entries)")
    ap.add_argument("--limit", type=int, default=15, help="max postings to score, 0 for no cap")
    ap.add_argument("--top", type=int, default=DEFAULT_TOP, help="shortlist size cap")
    ap.add_argument("--floor", type=int, default=SHORTLIST_FLOOR)
    ap.add_argument("--no-fetch", action="store_true", help="score stubs only, no detail calls")
    ap.add_argument("--max-fetch-per-portal", type=int, default=DEFAULT_MAX_FETCH_PER_PORTAL)
    ap.add_argument("--pace", type=float, default=DEFAULT_PACE, help="seconds between detail calls")
    ap.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "sweep-triage-cache")
    ap.add_argument("--out", type=Path, help="also write the report JSON here")
    args = ap.parse_args()
    if not args.input and not args.state:
        ap.error("one of --input or --state is required")

    posts = load_input_posts(args)
    eligible = len(posts)
    if args.limit > 0:
        posts = posts[: args.limit]

    attempts: dict[str, int] = {}
    failures: dict[str, int] = {}
    rows = [
        score_posting(p, not args.no_fetch, args.cache_dir, args.pace, attempts, failures, args.max_fetch_per_portal)
        for p in posts
    ]
    # --floor is accepted for before/after experiments; the committed default
    # stays SHORTLIST_FLOOR and the test pins it to /rank's Good Fit floor.
    full = sorted(
        [r for r in rows if r["path"] == "full_text" and r["score"] >= args.floor],
        key=lambda r: r["score"],
        reverse=True,
    )
    shortlist = full[: args.top] if args.top > 0 else full
    for r in rows:
        r["shortlisted"] = any(r is s for s in shortlist)
    needs_fetch = [
        {"key": r["key"], "title": r["title"], "company": r["company"], "url": r["url"],
         "score": r["score"], "band": r["band"]}
        for r in sorted(
            [r for r in rows if r["path"] == "stub" and r["band"].startswith("Stub: strong")],
            key=lambda r: r["score"],
            reverse=True,
        )
    ]
    report = {
        "meta": {
            "eligible": eligible,
            "scored": len(rows),
            "deferred": max(0, eligible - len(rows)),
            "full_text": sum(1 for r in rows if r["path"] == "full_text"),
            "stubs": sum(1 for r in rows if r["path"] == "stub"),
            "floor": args.floor,
            "full_saturation": FULL_SATURATION,
            "stub_saturation": STUB_SATURATION,
            "top": args.top,
        },
        "shortlist": [
            {k: r[k] for k in ("key", "title", "company", "url", "portal", "score", "band", "path", "text_source")}
            for r in shortlist
        ],
        "needs_fetch": needs_fetch,
        "postings": rows,
    }
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
