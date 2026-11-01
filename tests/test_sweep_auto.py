"""Tests for tools/sweep_auto.py - the autonomous scheduled sweep.

Pins the three behaviors the brief demands: dedupe by content hash (never
title matching), deterministic scoring on the standing weights, and the
absence of send/message/profile/secret code paths anywhere in the module.
"""
import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import sweep_auto  # noqa: E402
from sweep_auto import (  # noqa: E402
    append_tracker_rows,
    identity_full,
    identity_meta,
    is_due,
    is_seen,
    last_run,
    read_tracker_rows,
    record_run,
    run_sweep,
    seen_identities,
    triage,
    write_artifact,
)
from rank_state import band, overall_score  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "sweep_auto.py"
RUN_DATE = "2026-10-29"


def posting(**over):
    base = {
        "title": "AI Engineer",
        "company": "Certa",
        "location": "Remote (India)",
        "url": "https://wellfound.com/jobs/4496853-ai-engineer",
        "description": (
            "Own the agent loop: MCP servers, context engineering, "
            "golden-dataset evals gating every change. 3+ years production "
            "backend. Python, FastAPI, AWS."
        ),
        "portal": "wellfound",
        "posted_date": "2026-10-24",
    }
    base.update(over)
    return base


class ContentHashIdentity(unittest.TestCase):
    def test_case_whitespace_url_variants_match(self):
        a = posting()
        b = posting(title="  ai ENGINEER ", company="CERTA",
                    url="https://wellfound.com/jobs/4496853-ai-engineer?x=1#top")
        self.assertEqual(identity_meta(**{k: a[k] for k in ("company", "title")},
                                       location=a["location"], url=a["url"]),
                         identity_meta(**{k: b[k] for k in ("company", "title")},
                                       location=b["location"], url=b["url"]))
        self.assertEqual(identity_full(a["company"], a["title"], a["location"],
                                       a["url"], a["description"]),
                         identity_full(b["company"], b["title"], b["location"],
                                       b["url"], b["description"]))

    def test_same_title_different_company_differs(self):
        # Title matching would collapse these; the hash must not.
        a = identity_full("Certa", "AI Engineer", "Remote", "https://x/1", "agent evals")
        b = identity_full("Daxani", "AI Engineer", "Remote", "https://x/2", "agent evals")
        self.assertNotEqual(a, b)
        self.assertNotEqual(
            identity_meta("Certa", "AI Engineer", "Remote", "https://x/1"),
            identity_meta("Daxani", "AI Engineer", "Remote", "https://x/2"))

    def test_same_meta_different_body_differs_full_matches_meta(self):
        a = posting(description="agent loop, MCP servers, golden evals")
        b = posting(description="totally rewritten body about dashboards")
        self.assertNotEqual(
            identity_full(a["company"], a["title"], a["location"], a["url"], a["description"]),
            identity_full(b["company"], b["title"], b["location"], b["url"], b["description"]))
        self.assertEqual(
            identity_meta(a["company"], a["title"], a["location"], a["url"]),
            identity_meta(b["company"], b["title"], b["location"], b["url"]))

    def test_legacy_seen_entry_without_identity_still_dedupes(self):
        seen_doc = {"seen": {"certa_ai-engineer": {
            "title": "AI Engineer", "company": "Certa",
            "url": "https://wellfound.com/jobs/4496853-ai-engineer",
            "location": "Remote (India)", "status": "new"}}}
        meta, full = seen_identities(seen_doc)
        seen, _ = is_seen(posting(), meta, full)
        self.assertTrue(seen)

    def test_rerun_with_stored_identity_dedupes(self):
        results, entries, _ = run_sweep([posting()], {"seen": {}}, [], run_date=RUN_DATE)
        self.assertEqual(len(results), 1)
        seen_doc = {"seen": dict(entries)}
        results2, _, _ = run_sweep([posting()], seen_doc, [], run_date=RUN_DATE)
        self.assertEqual(results2, [])

    def test_tracker_pair_excluded(self):
        rows = [{"company": "Certa", "role": "AI ENGINEER"}]
        results, _, _ = run_sweep([posting()], {"seen": {}}, rows, run_date=RUN_DATE)
        self.assertEqual(results, [])

    def test_same_title_other_company_passes(self):
        rows = [{"company": "Certa", "role": "AI Engineer"}]
        other = posting(company="Daxani", url="https://wellfound.com/jobs/4872307-x")
        results, _, _ = run_sweep([other], {"seen": {}}, rows, run_date=RUN_DATE)
        self.assertEqual(len(results), 1)


class ScoringDeterminism(unittest.TestCase):
    def test_same_text_same_result(self):
        self.assertEqual(triage(posting()), triage(posting()))

    def test_overall_uses_standing_weights_and_bands(self):
        scored = triage(posting())
        self.assertEqual(scored["overall"], overall_score(scored["scores"]))
        self.assertEqual(scored["verdict"], band(scored["overall"]))
        self.assertEqual(set(scored["scores"]),
                         {"technical", "experience", "behavioral", "career"})

    def test_agentic_fit_outscores_unrelated(self):
        weak = posting(title="PHP Developer", company="Kontrah",
                       url="https://example.com/j/9",
                       description="WordPress themes, 1-2 years, onsite Mumbai.")
        self.assertGreater(triage(posting())["overall"], triage(weak)["overall"])

    def test_lead_title_caps_experience(self):
        lead = posting(title="Lead AI Engineer",
                       description="Lead a team of 8. 8+ years. Machine learning.")
        self.assertLessEqual(triage(lead)["scores"]["experience"], 40)

    def test_relocation_fail_kept_out_of_shortlist(self):
        far = posting(location="Bengaluru (onsite)",
                      description="Great agentic role. Must relocate to Bengaluru.")
        scored = triage(far)
        self.assertEqual(scored["location_verdict"], "FAIL")
        results, _, tracker = run_sweep([far], {"seen": {}}, [], run_date=RUN_DATE)
        self.assertEqual(len(results), 1)
        self.assertEqual(tracker, [])

    def test_results_sorted_descending(self):
        weak = posting(title="PHP Developer", company="Kontrah",
                       url="https://example.com/j/9",
                       description="WordPress themes, onsite Mumbai.")
        results, _, _ = run_sweep([weak, posting()], {"seen": {}}, [], run_date=RUN_DATE)
        self.assertGreaterEqual(results[0]["overall"], results[1]["overall"])

    def test_tracker_row_uses_open_unsent_status(self):
        _, _, tracker = run_sweep([posting()], {"seen": {}}, [], run_date=RUN_DATE)
        self.assertEqual(len(tracker), 1)
        row = tracker[0]
        self.assertEqual(row["status"], "drafted")
        self.assertEqual(row["channel"], "sweep")
        self.assertIn("sweep-auto", row["notes"])
        self.assertEqual(set(row), set(sweep_auto.TRACKER_HEADER.split(",")))


class SchedulerState(unittest.TestCase):
    def test_no_state_file_means_due(self):
        self.assertTrue(is_due(today="2026-10-29",
                               state_path=Path("/nonexistent/state.json")))

    def test_fresh_run_not_due_old_run_due(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            record_run("2026-10-29", state)
            self.assertEqual(last_run(state), "2026-10-29")
            self.assertFalse(is_due(14, today="2026-10-30", state_path=state))
            self.assertTrue(is_due(14, today="2026-11-13", state_path=state))

    def test_malformed_state_means_due(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            state.write_text('{"last_run": "someday"}', encoding="utf-8")
            self.assertIsNone(last_run(state))
            self.assertTrue(is_due(today="2026-10-29", state_path=state))

    def test_artifact_and_tracker_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            results, _, tracker = run_sweep([posting()], {"seen": {}}, [],
                                            run_date=RUN_DATE)
            artifact = write_artifact(results, RUN_DATE, tmp / "sweeps")
            self.assertEqual(artifact.name, f"sweep-{RUN_DATE}.md")
            text = artifact.read_text(encoding="utf-8")
            self.assertIn("Certa", text)
            tracker_path = append_tracker_rows(tracker, tmp / "tracker.csv")
            rows = read_tracker_rows(tracker_path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "drafted")
            self.assertTrue(tracker_path.read_text(encoding="utf-8").splitlines()[0]
                            .endswith(",deadline"))


class DiscoveryStaysReadOnly(unittest.TestCase):
    def test_commands_are_search_only_on_known_clis(self):
        known = {".agents/skills/freehire-search/cli/src/cli.ts",
                 ".agents/skills/linkedin-search/cli/src/cli.ts"}
        for cmd in sweep_auto.freehire_commands() + sweep_auto.linkedin_commands():
            self.assertEqual(cmd[:2], ["bun", "run"])
            self.assertIn(cmd[2], known)
            self.assertEqual(cmd[3], "search")

    def test_queries_cover_report_shapes(self):
        queries = [q for q, _ in sweep_auto.FREEHIRE_QUERIES]
        for expected in ("AI engineer", "Agentic AI engineer", "GenAI engineer",
                         "LLM engineer", "MLOps engineer"):
            self.assertIn(expected, queries)
        self.assertTrue(any("cutshort.io/jobs/" in u for u in sweep_auto.CUTSHORT_LISTINGS))
        self.assertTrue(any("wellfound.com/role/l/" in u for u in sweep_auto.WELLFOUND_LISTINGS))


FORBIDDEN_PROSE = [r"\bsubmit\w*", r"\bsign-?up\b", r"\bpassword\b",
                   r"\bcredential\w*", r"\blog-?in\b", r"\bsmtp\b",
                   r"\bwebhook\b", r"\bbearer\b", r"\bnaukri\b",
                   r"\bPOST\b"]
# Identifier-level: no send/message/profile/secret code paths. String
# literals are prose (queries, CSV header, notes), so only identifier nodes
# are checked - the "Applied AI engineer" query and the contact_person
# column must not trip this.
FORBIDDEN_IDENTIFIERS = [r"^submit", r"^appl(y|ied|ication)",
                         r"^contact$", r"^contacts$", r"^email", r"^mail_to",
                         r"^sign_?up", r"^create_account", r"^register",
                         r"^login", r"^logout", r"^password", r"^credential",
                         r"^secret", r"^api_key", r"^bearer", r"^send_",
                         r"^notify_external"]


def _identifiers(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.append(node.name)
        elif isinstance(node, ast.Name):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            found.append(node.attr)
        elif isinstance(node, ast.arg):
            found.append(node.arg)
    return found


class GuardrailAbsence(unittest.TestCase):
    def test_no_forbidden_prose_tokens(self):
        text = TOOL.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PROSE:
            hits = re.findall(pattern, text, re.IGNORECASE)
            self.assertEqual(hits, [], f"forbidden token {pattern}: {hits}")

    def test_no_forbidden_code_paths(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        names = _identifiers(tree)
        bad = [n for n in names
               if any(re.match(p, n, re.IGNORECASE) for p in FORBIDDEN_IDENTIFIERS)]
        self.assertEqual(bad, [], f"forbidden code paths: {bad}")

    def test_imports_are_stdlib_or_state_helpers(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        allowed = {"argparse", "csv", "datetime", "hashlib", "json", "re",
                   "subprocess", "sys", "pathlib", "urllib", "rank_state"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertIn(alias.name.split(".")[0], allowed, alias.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertIn((node.module or "").split(".")[0], allowed, node.module)

    def test_no_write_side_network_calls(self):
        tree = ast.parse(TOOL.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (func.attr if isinstance(func, ast.Attribute)
                        else func.id if isinstance(func, ast.Name) else "")
                self.assertNotIn(name, {"post", "put", "patch", "delete", "request"},
                                 f"write-side network call: {name}")
                if name in {"urlopen", "Request"}:
                    keywords = {kw.arg for kw in node.keywords}
                    self.assertNotIn("data", keywords, "urlopen/Request with a body")
                    for kw in node.keywords:
                        if kw.arg == "method" and isinstance(kw.value, ast.Constant):
                            self.assertEqual(kw.value.value, "GET")


if __name__ == "__main__":
    unittest.main()
