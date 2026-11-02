"""Tests for tools/sweep_triage.py - honest sweep triage scoring.

The bug: a 15-posting sweep shortlisted 0 because keyword-counting on
title-plus-snippet stubs cannot clear a bar built for full descriptions.
The fix has three layers, each pinned here:

1. Full text first (detail fetch / search-hydrated text scored on the
   full-text scale, with per-posting provenance).
2. Stub fallback (stubs score on their own scale with Stub: bands and are
   never shortlisted, no matter their score).
3. Vocabulary (core agentic/RAG/eval terms highest-weighted, adjacent
   production-AI/ML next - weight sanity pinned on fixtures).

The floor contract: SHORTLIST_FLOOR stays 60, equal to /rank's Good Fit
floor. It moves only with stated before/after evidence on the fixed
fixture set below - the committed table IS that evidence.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sweep_triage as triage  # noqa: E402
from rank_state import BANDS  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "sweep_triage.py"
FIXTURES = REPO / "tests" / "fixtures" / "sweep_triage" / "postings.json"


def load_fixtures() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def strip_full_text(posts: list[dict]) -> list[dict]:
    return [{**p, "full_text": ""} for p in posts]


def blank() -> tuple[dict, dict]:
    return {}, {}


# The committed before/after evidence on the fixed fixture set, produced by
# `python3 tools/sweep_triage.py score --input <fixtures> --no-fetch --top 0`
# (postings carry real full text, so --no-fetch still scores full_text via
# the search_hydrated path). key -> (score, band, shortlisted).
EXPECTED_AFTER = {
    "ai-engineer-novapay-qc7u574u": (100, "Strong Fit", True),
    "ai-engineer-senovo-it-pnv2kisp": (100, "Strong Fit", True),
    "ai-engineer-accuity-ozvw4yoc": (100, "Strong Fit", True),
    "ai-engineer-deepscribe-brfabs4e": (73, "Good Fit", True),
    "ai-engineer-servicetitan-xyatsdq5": (60, "Good Fit", True),
    "ai-engineer-servicetitan-sh6rmyxc": (100, "Strong Fit", True),
    "machine-learning-engineer-allcloud-zdeurlcc": (73, "Good Fit", True),
    "machine-learning-engineer-coinbase-latkv4qv": (100, "Strong Fit", True),
    "machine-learning-engineer-stackadapt-mfnenlpf": (47, "Moderate Fit", False),
    "machine-learning-engineer-elinext-eatishhl": (83, "Strong Fit", True),
    "machine-learning-engineer-north-sails-jrd6qal3": (40, "Weak Fit", False),
    "registered-nurse-community-health-center-of-southeast-kansas-b26adqa7": (0, "Poor Fit", False),
    "registered-nurse-elite-virtual-brokerage-gzeyhgq7": (7, "Poor Fit", False),
    "registered-nurse-caring-hand-home-health-solutions-inc-2et2qk3n": (0, "Poor Fit", False),
    "registered-nurse-ansiblehealth-by37ctz5": (0, "Poor Fit", False),
}


class FixtureIntegrity(unittest.TestCase):
    def test_fixture_set_is_fixed_and_real(self):
        posts = load_fixtures()
        self.assertEqual(15, len(posts), "the 15-posting sweep fixture set changed size")
        for p in posts:
            for field in ("key", "title", "company", "url", "portal", "snippet", "full_text", "detail_id"):
                self.assertIn(field, p, f"{p.get('key')}: missing {field}")
            self.assertTrue((p["full_text"] or "").strip(), f"{p['key']}: full text is empty")

    def test_fixtures_cover_both_stub_shapes(self):
        posts = load_fixtures()
        with_snippet = [p for p in posts if (p.get("snippet") or "").strip()]
        title_only = [p for p in posts if not (p.get("snippet") or "").strip()]
        self.assertGreater(len(with_snippet), 0, "no preview-snippet stubs covered")
        self.assertGreater(len(title_only), 0, "no title-only stubs covered")


class FloorContract(unittest.TestCase):
    def test_shortlist_floor_is_60(self):
        self.assertEqual(60, triage.SHORTLIST_FLOOR)

    def test_floor_matches_rank_good_fit_floor(self):
        good_fit_floor = next(floor for floor, name in BANDS if name == "Good Fit")
        self.assertEqual(
            good_fit_floor,
            triage.SHORTLIST_FLOOR,
            "the triage bar drifted from /rank's Good Fit bar - move both with evidence or neither",
        )

    def test_full_text_bands_come_from_the_rank_scale(self):
        self.assertEqual("Good Fit", triage.rank_band(60))
        self.assertEqual("Moderate Fit", triage.rank_band(59))
        self.assertEqual("Strong Fit", triage.rank_band(75))


class FullTextPath(unittest.TestCase):
    def test_full_text_scores_on_the_full_scale_with_provenance(self):
        posts = load_fixtures()
        attempts, failures = blank()
        row = triage.score_posting(posts[3], False, Path("/nonexistent"), 0, attempts, failures, 10)
        self.assertEqual("full_text", row["path"])
        self.assertEqual("search_hydrated", row["text_source"])
        self.assertEqual(73, row["score"])
        self.assertEqual("Good Fit", row["band"])
        self.assertTrue(row["shortlisted"])

    def test_below_floor_full_text_is_not_shortlisted(self):
        posts = {p["key"]: p for p in load_fixtures()}
        attempts, failures = blank()
        row = triage.score_posting(
            posts["machine-learning-engineer-stackadapt-mfnenlpf"],
            False, Path("/nonexistent"), 0, attempts, failures, 10,
        )
        self.assertEqual(("full_text", 47, "Moderate Fit", False),
                         (row["path"], row["score"], row["band"], row["shortlisted"]))

    def test_no_fetch_still_scores_known_full_text(self):
        # --no-fetch disables detail calls, not already-known text.
        posts = load_fixtures()
        attempts, failures = blank()
        for p in posts:
            row = triage.score_posting(p, False, Path("/nonexistent"), 0, attempts, failures, 10)
            self.assertEqual("full_text", row["path"], p["key"])
        self.assertEqual({}, attempts, "no detail call should have been attempted")


class StubFallback(unittest.TestCase):
    def test_stub_rows_carry_stub_bands_and_are_never_shortlisted(self):
        posts = strip_full_text(load_fixtures())
        attempts, failures = blank()
        for p in posts:
            row = triage.score_posting(p, False, Path("/nonexistent"), 0, attempts, failures, 10)
            self.assertEqual("stub", row["path"], p["key"])
            self.assertEqual("stub", row["text_source"], p["key"])
            self.assertTrue(row["band"].startswith("Stub:"), f"{p['key']}: {row['band']}")
            self.assertFalse(row["shortlisted"], f"{p['key']}: a stub must never shortlist")

    def test_even_a_perfect_stub_score_never_shortlists(self):
        # DeepScribe's preview snippet is keyword-rich: stub score hits 100.
        # Structure, not arithmetic, keeps it out of the shortlist.
        posts = {p["key"]: p for p in strip_full_text(load_fixtures())}
        attempts, failures = blank()
        row = triage.score_posting(
            posts["ai-engineer-deepscribe-brfabs4e"], False, Path("/nonexistent"), 0, attempts, failures, 10
        )
        self.assertEqual(100, row["score"])
        self.assertEqual("stub", row["path"])
        self.assertFalse(row["shortlisted"])

    def test_stub_bands_rank_fetch_priority(self):
        attempts, failures = blank()
        cases = {
            "AI Engineer": "Stub: strong signal",
            "Machine Learning Engineer": "Stub: weak signal",
            "Registered Nurse": "Stub: no signal",
        }
        for title, band_prefix in cases.items():
            row = triage.score_posting(
                {"title": title, "company": "X"}, False, Path("/nonexistent"), 0, attempts, failures, 10
            )
            self.assertTrue(
                row["band"].startswith(band_prefix), f"{title}: got {row['band']}"
            )


class VocabularyWeights(unittest.TestCase):
    def test_tier_weights_keep_niche_terms_highest(self):
        weights = {name: weight for name, weight, _ in triage.TIERS}
        self.assertEqual({"core": 3, "adjacent": 2, "general": 1}, weights)

    def test_agentic_posting_outscores_plain_backend_posting(self):
        agentic = "AI Engineer building LLM agents with RAG, LangChain and evals on Kubernetes"
        backend = "Backend Engineer with Java, Spring Boot, microservices and system design"
        a_points, _ = triage.weighted_points(agentic)
        b_points, _ = triage.weighted_points(backend)
        self.assertGreater(a_points, b_points * 2)

    def test_matching_is_token_exact_not_substring(self):
        # Bare 'ai' must match the token AI, never 'said' or 'OpenAI'.
        points, signals = triage.weighted_points("He said OpenAI trains models")
        self.assertNotIn("ai", signals["adjacent"])
        self.assertNotIn("ai", signals["core"])
        self.assertEqual(0, points)

    def test_adjacent_recall_without_niche_terms(self):
        # An ML-platform posting with no agentic words still scores adjacent.
        points, signals = triage.weighted_points(
            "Machine Learning Engineer: model serving, feature store, PyTorch, Airflow"
        )
        self.assertEqual([], signals["core"])
        self.assertGreaterEqual(points, 2 * 4)


class BeforeAfterEvidence(unittest.TestCase):
    def test_before_narrow_vocab_on_stubs_shortlists_zero(self):
        # The reported bug, reproduced on the fixed fixture set: the old
        # niche-only vocabulary scored on stubs against the 60 bar.
        shortlisted = 0
        for p in strip_full_text(load_fixtures()):
            found = triage.matched_phrases(triage.stub_text(p), triage.CORE_PHRASES)
            score = triage.score_on_scale(3 * len(found), triage.FULL_SATURATION)
            if score >= triage.SHORTLIST_FLOOR:
                shortlisted += 1
        self.assertEqual(0, shortlisted)

    def test_after_full_text_with_broad_vocab_matches_committed_table(self):
        posts = load_fixtures()
        attempts, failures = blank()
        rows = [
            triage.score_posting(p, False, Path("/nonexistent"), 0, attempts, failures, 10)
            for p in posts
        ]
        self.assertEqual(set(EXPECTED_AFTER), {r["key"] for r in rows})
        for r in rows:
            expected = EXPECTED_AFTER[r["key"]]
            self.assertEqual(
                expected, (r["score"], r["band"], r["shortlisted"]),
                f"before/after table drifted for {r['key']} - update the table only with new stated evidence",
            )

    def test_after_shortlists_nine_with_five_below_or_out(self):
        shortlisted = [k for k, (_, _, s) in EXPECTED_AFTER.items() if s]
        self.assertEqual(9, len(shortlisted))
        self.assertIn("machine-learning-engineer-stackadapt-mfnenlpf",
                      [k for k, (_, _, s) in EXPECTED_AFTER.items() if not s])


class FetchLayer(unittest.TestCase):
    def test_detail_id_wins_over_url(self):
        self.assertEqual(
            "some-slug",
            triage.detail_target({"detail_id": "some-slug", "url": "https://example.com/job"}),
        )
        self.assertEqual(
            "https://example.com/job", triage.detail_target({"url": "https://example.com/job"})
        )

    def test_detail_command_rejects_untrusted_portals(self):
        self.assertIsNone(triage.detail_command("../../etc", "x"))
        self.assertIsNone(triage.detail_command("no-such-portal", "x"))
        self.assertIsNone(triage.detail_command("freehire-search", ""))

    def test_successful_fetch_scores_full_text_and_caches(self):
        import tempfile

        calls = []

        def fake_run_detail(cmd):
            calls.append(cmd)
            return "AI Engineer with LLM agents and RAG evaluation"

        real = triage.run_detail
        triage.run_detail = fake_run_detail
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cache = Path(tmp)
                posting = {"key": "k", "title": "AI Engineer", "portal": "freehire-search",
                           "url": "https://example.com/1", "detail_id": "slug-1"}
                attempts, failures = blank()
                first = triage.score_posting(posting, True, cache, 0, attempts, failures, 10)
                second = triage.score_posting(posting, True, cache, 0, attempts, failures, 10)
        finally:
            triage.run_detail = real
        self.assertEqual("full_text", first["path"])
        self.assertEqual("detail_fetch", first["text_source"])
        self.assertEqual("detail_cache", second["text_source"])
        self.assertEqual(1, len(calls), "second run must be served from cache")
        self.assertEqual(first["score"], second["score"])

    def test_fetch_failure_degrades_to_stub_without_failing_the_sweep(self):
        real = triage.run_detail
        triage.run_detail = lambda cmd: None
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                attempts, failures = blank()
                bad = {"key": "bad", "title": "AI Engineer", "portal": "freehire-search",
                       "url": "https://example.com/bad", "detail_id": "bad-slug"}
                good = {"key": "good", "title": "AI Engineer", "company": "C",
                        "full_text": "LLM agents with RAG, LangChain, evals, vector database, MCP"}
                bad_row = triage.score_posting(bad, True, Path(tmp), 0, attempts, failures, 10)
                good_row = triage.score_posting(good, True, Path(tmp), 0, attempts, failures, 10)
        finally:
            triage.run_detail = real
        self.assertEqual("stub", bad_row["path"])
        self.assertIn("fetch_note", bad_row)
        self.assertFalse(bad_row["shortlisted"])
        self.assertEqual("full_text", good_row["path"])
        self.assertTrue(good_row["shortlisted"])

    def test_per_portal_cap_and_circuit_breaker_bound_fetches(self):
        real = triage.run_detail
        triage.run_detail = lambda cmd: None
        try:
            import tempfile

            with tempfile.TemporaryDirectory() as tmp:
                attempts, failures = blank()
                for i in range(5):
                    triage.score_posting(
                        {"key": f"k{i}", "title": "T", "portal": "freehire-search",
                         "url": f"https://example.com/{i}", "detail_id": f"s{i}"},
                        True, Path(tmp), 0, attempts, failures, 10,
                    )
        finally:
            triage.run_detail = real
        # Two failed attempts trip the breaker; the rest never run.
        self.assertEqual({"freehire-search": 2}, attempts)


class ProvenanceLabels(unittest.TestCase):
    def test_every_row_labels_which_path_it_took(self):
        posts = load_fixtures()
        attempts, failures = blank()
        mixed = [{**posts[0]}, {**strip_full_text(posts)[-1]}]
        rows = [triage.score_posting(p, False, Path("/nonexistent"), 0, attempts, failures, 10)
                for p in mixed]
        by_key = {r["key"]: r for r in rows}
        self.assertEqual(("full_text", "search_hydrated"),
                         (by_key[posts[0]["key"]]["path"], by_key[posts[0]["key"]]["text_source"]))
        last = posts[-1]
        self.assertEqual(("stub", "stub"),
                         (by_key[last["key"]]["path"], by_key[last["key"]]["text_source"]))

    def test_full_text_bands_never_carry_the_stub_prefix(self):
        posts = load_fixtures()
        attempts, failures = blank()
        for p in posts:
            row = triage.score_posting(p, False, Path("/nonexistent"), 0, attempts, failures, 10)
            self.assertFalse(row["band"].startswith("Stub:"), p["key"])


class ToolCli(unittest.TestCase):
    def test_score_command_reports_shortlist_and_needs_fetch(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL), "score", "--input", str(FIXTURES),
             "--no-fetch", "--top", "5"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(15, report["meta"]["scored"])
        self.assertEqual(15, report["meta"]["full_text"])
        self.assertEqual(5, len(report["shortlist"]))
        self.assertTrue(all(s["score"] >= 60 for s in report["shortlist"]))

    def test_stub_only_run_shortlists_nothing_but_names_fetches(self):
        import tempfile

        posts = strip_full_text(load_fixtures())
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(posts, fh)
            path = fh.name
        proc = subprocess.run(
            [sys.executable, str(TOOL), "score", "--input", path, "--no-fetch", "--top", "0"],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(0, proc.returncode, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual([], report["shortlist"])
        self.assertGreater(len(report["needs_fetch"]), 0)
        self.assertTrue(all(r["path"] == "stub" for r in report["postings"]))


if __name__ == "__main__":
    unittest.main()
