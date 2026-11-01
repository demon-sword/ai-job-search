"""Tests for tools/linkedin_connect.py - the connection-request bot.

Every safety rail from the delivery spec gets a pinned case here, all
network-free: the live browser is behind the InviteDriver interface, so
these inject scripted fakes instead. Subprocess cases cover the real CLI
surface (plan/run/status exit codes and outputs); in-process cases cover
the live send loop, which needs a driver no subprocess can inject.
"""
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.linkedin_connect import (
    DISCOVERY_STATUSES,
    TERMINAL_STATUSES,
    CaptchaChallenge,
    InviteDriver,
    InviteStop,
    LoginChallenge,
    RateLimited,
    UnexpectedUI,
    VerificationPrompt,
    append_audit,
    build_parser,
    cache_key,
    canonical_key,
    check_note,
    classify_interstitial,
    cmd_discover,
    cmd_run,
    discovery_defaults,
    discovery_queries,
    effective_cap,
    inside_window,
    load_cache,
    load_ledger,
    pace_delay,
    queued_today,
    run_discover,
    run_queue,
    save_cache,
    save_ledger,
    score_candidate,
    scrub,
    sends_today,
)

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "linkedin_connect.py"
TARGETS = REPO / "tools" / "linkedin_connect_targets.json"
DAY = "2026-11-01"
NOW = f"{DAY}T10:00:00+05:30"
ALWAYS = "00:00-23:59"  # window that always contains local time
NEVER = "00:00-00:00"  # empty window: inside_window is always False


def cand(slug, headline="Tech Recruiter, GenAI hiring", location="Remote", name=None):
    return {
        "profile_url": f"https://www.linkedin.com/in/{slug}/",
        "name": name or slug,
        "headline": headline,
        "location": location,
    }


class FakeDriver(InviteDriver):
    """Scripted driver: each send pops the next outcome, or raises it."""

    def __init__(self, script=None, searches=None, views=None):
        self.script = list(script or ["sent"])
        self.searches = list(searches or [])
        self.views = list(views or [])
        self.calls = []
        self.search_calls = []
        self.view_calls = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def send_invite(self, candidate, note):
        self.calls.append((dict(candidate), note))
        item = self.script.pop(0) if self.script else "sent"
        if isinstance(item, Exception):
            raise item
        return item

    def search_people(self, keywords, location, page):
        self.search_calls.append((keywords, location, page))
        item = self.searches.pop(0) if self.searches else []
        if isinstance(item, Exception):
            raise item
        return item

    def view_profile(self, profile_url):
        self.view_calls.append(profile_url)
        item = self.views.pop(0) if self.views else {}
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def search_card(slug, headline="Tech Recruiter, GenAI hiring", location="Bengaluru, India", name=None):
    return {
        "profile_url": f"https://www.linkedin.com/in/{slug}/",
        "name": name or slug,
        "headline": headline,
        "location": location,
    }


def seed_query(keywords="recruiter genai India", location="India"):
    return {"keywords": keywords, "role": "recruiter", "domain": "genai", "location": location}


def mini_targets(tmp, **over):
    """Small discovery config so CLI tests run 2 seeded queries, not 252."""
    cfg = {
        "min_score": 4,
        "role_signals": [{"match": "recruiter", "weight": 4, "tag": "recruiter"}],
        "domain_weight": 1,
        "domain_signals": ["genai"],
        "location_weight": 1,
        "location_allow": ["india", "remote"],
        "discovery": {
            "max_searches": 2,
            "max_pages": 2,
            "max_profile_views": 5,
            "max_queued_per_day": 20,
            "search_min_delay": 0,
            "search_max_delay": 0,
            "role_seeds": ["recruiter", "hiring manager"],
            "locations": ["India"],
        },
    }
    cfg["discovery"].update(over.get("discovery", {}))
    path = tmp / "targets.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


class BotCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state = self.tmp / "state"
        self.audit = self.state / "audit.jsonl"
        self.ledger = {"persons": {}, "sends": []}
        self.cfg = json.loads(TARGETS.read_text(encoding="utf-8"))

    def run_live(self, candidates, **over):
        kw = dict(
            live=True, cap=20, day=DAY, window=ALWAYS, note=None, limit=0,
            min_delay=0, max_delay=0, now_iso=NOW,
        )
        kw.update(over)
        factory = kw.pop("factory", None)
        if factory is None:
            driver = FakeDriver(kw.pop("script", ["sent"]))
            factory = lambda: driver  # noqa: E731
            self.driver = driver
        return run_queue(candidates, self.cfg, self.ledger, self.audit, driver_factory=factory, **kw)

    def audit_rows(self):
        if not self.audit.is_file():
            return []
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]


class Targeting(BotCase):
    def test_recruiter_hiring_manager_and_eng_leader_score_above_threshold(self):
        cases = [
            cand("a", headline="Tech Recruiter | AI hiring"),
            cand("b", headline="Hiring Manager, Data Platform"),
            cand("c", headline="Engineering Manager, ML"),
        ]
        for c in cases:
            with self.subTest(c["headline"]):
                score, reasons = score_candidate(c, self.cfg)
                self.assertGreaterEqual(score, self.cfg["min_score"], reasons)

    def test_unrelated_role_scores_below_threshold(self):
        score, _ = score_candidate(cand("x", headline="Sales Executive, FMCG", location="London"), self.cfg)
        self.assertLess(score, self.cfg["min_score"])

    def test_criteria_are_config_data_not_hardcoded_names(self):
        """The same candidate passes under one --targets file and fails under
        another, so selection cannot be smuggling in fixed names."""
        person = cand("someone", headline="Tech Recruiter, GenAI", location="Remote")
        score_default, _ = score_candidate(person, self.cfg)
        strict = dict(self.cfg, min_score=score_default + 10)
        score_strict, _ = score_candidate(person, strict)
        self.assertGreaterEqual(score_default, self.cfg["min_score"])
        self.assertLess(score_strict, strict["min_score"])
        renamed = dict(self.cfg, role_signals=[{"match": "zyxq-plugh", "weight": 99, "tag": "x"}],
                       domain_signals=[], location_allow=[])
        score_renamed, _ = score_candidate(person, renamed)
        self.assertLess(score_renamed, self.cfg["min_score"])

    def test_remote_ncr_lens_scores_home_markets(self):
        for location in ["Remote", "Delhi NCR", "Noida, India", "Bengaluru, India"]:
            score, reasons = score_candidate(
                cand("p", headline="AI Engineer", location=location), self.cfg)
            with self.subTest(location):
                self.assertTrue(any(r.startswith("location:") for r in reasons), reasons)


class CanonicalKey(BotCase):
    def test_in_slug_is_lowercased_and_query_stripped(self):
        self.assertEqual(
            canonical_key("https://www.linkedin.com/in/Jane-Doe-123/?trk=public_profile"),
            "in:jane-doe-123",
        )

    def test_trailing_slash_variants_collide(self):
        self.assertEqual(
            canonical_key("https://www.linkedin.com/in/jane/"),
            canonical_key("https://uk.linkedin.com/in/jane"),
        )

    def test_blank_url_is_unkeyable(self):
        for bad in ["", "   "]:
            with self.subTest(repr(bad)):
                with self.assertRaises(ValueError):
                    canonical_key(bad)

    def test_garbage_url_gets_a_stable_key_instead_of_colliding(self):
        """A non-profile URL still dedupes deterministically rather than
        raising (which would skip it silently every run) or collapsing onto
        someone else's key."""
        key = canonical_key("not a url at all -->")
        self.assertEqual(key, canonical_key("not a url at all -->"))
        self.assertNotEqual(key, canonical_key("https://www.linkedin.com/in/jane/"))


class DailyCap(BotCase):
    def test_flag_above_twenty_is_refused_not_clamped(self):
        with self.assertRaises(ValueError):
            effective_cap(21)

    def test_flag_below_one_is_refused(self):
        with self.assertRaises(ValueError):
            effective_cap(0)

    def test_lower_flag_is_honoured(self):
        summary = self.run_live([cand(f"p{i}") for i in range(5)], cap=2)
        self.assertEqual(summary["sent"], 2)
        self.assertEqual(summary["stopped"], "cap_reached")
        self.assertEqual(len(self.driver.calls), 2)

    def test_twenty_sends_already_today_blocks_the_twenty_first(self):
        self.ledger["sends"] = [f"{DAY}T09:{i:02d}:00+05:30" for i in range(20)]
        summary = self.run_live([cand("late")])
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(summary["stopped"], "cap_reached")
        self.assertEqual(self.driver.calls, [], "cap hit must not even start a send")

    def test_yesterdays_sends_do_not_count(self):
        self.ledger["sends"] = ["2026-10-31T23:59:00+05:30"] * 20
        summary = self.run_live([cand("fresh")])
        self.assertEqual(summary["sent"], 1)

    def test_ledger_survives_a_restart(self):
        """Save mid-day, reload in a fresh dict (a new process), and the cap
        and dedupe still hold - the count lives in the file, not memory."""
        first = self.run_live([cand("one"), cand("two")])
        self.assertEqual(first["sent"], 2)
        path = self.state / "ledger.json"
        save_ledger(path, self.ledger)
        reloaded = load_ledger(path)
        self.assertEqual(sends_today(reloaded, DAY), 2)
        second = run_queue(
            [cand("one"), cand("two"), cand("three")], self.cfg, reloaded, self.audit,
            live=True, cap=20, day=DAY, window=ALWAYS, note=None, limit=0,
            min_delay=0, max_delay=0, driver_factory=lambda: FakeDriver(["sent"]),
            now_iso=NOW,
        )
        self.assertEqual(second["sent"], 1, "the two known persons dedupe; only the new one sends")
        self.assertEqual([r["profile"] for r in second["decisions"] if r["decision"] == "send"],
                         ["in:three"])


class DryRun(BotCase):
    def test_dry_run_sends_nothing_and_never_builds_a_driver(self):
        def exploding_factory():
            raise AssertionError("dry-run must not create a browser driver")

        summary = run_queue(
            [cand("a"), cand("b")], self.cfg, self.ledger, self.audit,
            live=False, cap=20, day=DAY, window=ALWAYS, note=None, limit=0,
            min_delay=0, max_delay=0, driver_factory=exploding_factory, now_iso=NOW,
        )
        self.assertEqual(summary["would_send"], 2)
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(self.ledger, {"persons": {}, "sends": []}, "ledger untouched")

    def test_dry_run_logs_exactly_what_it_would_send(self):
        run_queue([cand("a"), cand("x", headline="Sales", location="London")],
                  self.cfg, self.ledger, self.audit, live=False, cap=20, day=DAY,
                  window=ALWAYS, note=None, limit=0, min_delay=0, max_delay=0, now_iso=NOW)
        rows = self.audit_rows()
        self.assertEqual([r["decision"] for r in rows], ["dry-run", "skip"])
        self.assertEqual(rows[0]["profile"], "in:a")
        self.assertIn("role:recruiter", rows[0]["reason"])

    def test_cli_defaults_to_dry_run(self):
        queue = self.tmp / "q.json"
        queue.write_text(json.dumps([cand("a")]), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOL), "run", "--candidates", str(queue),
             "--state-dir", str(self.state), "--window", ALWAYS],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WOULD SEND", proc.stdout)
        self.assertIn("dry-run", proc.stdout)
        self.assertFalse((self.state / "ledger.json").exists(), "dry-run must not create a ledger")


class StopConditions(BotCase):
    def test_classifier_maps_each_danger_state(self):
        cases = [
            ("https://www.linkedin.com/checkpoint/challenge/", "Verify", "", VerificationPrompt),
            ("https://www.linkedin.com/in/x/", "LinkedIn", "please verify it's you", VerificationPrompt),
            ("https://www.linkedin.com/in/x/", "Captcha check", "complete the captcha", CaptchaChallenge),
            ("https://www.linkedin.com/in/x/", "LinkedIn", "press & hold to confirm", CaptchaChallenge),
            ("https://www.linkedin.com/authwall", "Sign in", "sign in password", LoginChallenge),
            ("https://www.linkedin.com/login", "Sign in", "email or phone password sign in", LoginChallenge),
            ("https://www.linkedin.com/in/x/", "LinkedIn", "you've reached your weekly invitation limit", RateLimited),
            ("https://www.linkedin.com/in/x/", "LinkedIn", "invitation limit reached, try again later", RateLimited),
            ("https://www.linkedin.com/in/x/", "Error", "request failed with http 999", RateLimited),
        ]
        for url, title, body, kind in cases:
            with self.subTest(kind=kind.signal, body=body[:40]):
                stop = classify_interstitial(url, title, body)
                self.assertIsInstance(stop, kind, f"no {kind.signal} raised for {url} {body!r}")

    def test_classifier_stays_quiet_on_a_normal_profile(self):
        self.assertIsNone(classify_interstitial(
            "https://www.linkedin.com/in/jane/", "Jane | LinkedIn",
            "Jane\nTech Recruiter\nConnect\nMessage"))

    def test_each_stop_signal_aborts_with_no_retry_and_no_further_sends(self):
        signals = [
            LoginChallenge("authwall"), CaptchaChallenge("captcha"),
            VerificationPrompt("challenge"), RateLimited("limit"),
            UnexpectedUI("no connect button"),
        ]
        for signal in signals:
            with self.subTest(signal=signal.signal):
                self.setUp()
                with self.assertRaises(InviteStop):
                    self.run_live([cand("first"), cand("second"), cand("third")],
                                  script=["sent", signal, "sent"])
                self.assertEqual(len(self.driver.calls), 2,
                                 "the failed invite is not retried and the third is never attempted")
                self.assertTrue(self.driver.closed, "driver is closed on the way out")

    def test_stopped_run_records_the_signal_in_the_audit_log(self):
        with self.assertRaises(InviteStop):
            self.run_live([cand("victim")], script=[CaptchaChallenge("solve this")])
        rows = self.audit_rows()
        self.assertEqual(rows[-1]["decision"], "stop")
        self.assertIn("captcha", rows[-1]["reason"])

    def test_cli_returns_exit_2_and_reports_loudly_on_a_stop(self):
        from tools import linkedin_connect as mod

        queue = self.tmp / "q.json"
        queue.write_text(json.dumps([cand("a"), cand("b")]), encoding="utf-8")
        real_driver = mod.PlaywrightDriver
        mod.PlaywrightDriver = lambda profile_dir: FakeDriver([RateLimited("weekly limit")])
        try:
            args = build_parser().parse_args([
                "run", "--candidates", str(queue), "--state-dir", str(self.state),
                "--window", ALWAYS, "--live",
            ])
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cmd_run(args)
        finally:
            mod.PlaywrightDriver = real_driver
        self.assertEqual(rc, 2)
        self.assertIn("BOT STOPPED", err.getvalue())
        self.assertIn("rate-limit", err.getvalue().lower())
        saved = load_ledger(self.state / "ledger.json")
        self.assertEqual(saved["persons"], {}, "a stopped run still persists what it knew")
        rows = self.audit_rows()
        self.assertEqual(rows[-1]["decision"], "stop")

    def test_already_contacted_outcomes_record_without_counting_cap(self):
        summary = self.run_live(
            [cand("p"), cand("c"), cand("s")],
            script=["already_pending", "already_connected", "sent"],
        )
        self.assertEqual(summary["sent"], 1)
        persons = self.ledger["persons"]
        self.assertEqual(persons["in:p"]["status"], "invited")
        self.assertEqual(persons["in:c"]["status"], "connected")
        self.assertEqual(len(self.ledger["sends"]), 1, "only the real send counts toward the cap")


class Dedupe(BotCase):
    def test_every_terminal_status_is_never_requeued(self):
        for status in sorted(TERMINAL_STATUSES):
            with self.subTest(status=status):
                self.setUp()
                self.ledger["persons"] = {"in:known": {"status": status, "last_ts": NOW}}
                summary = self.run_live([cand("known")])
                self.assertEqual(summary["sent"], 0)
                self.assertEqual(summary["skipped"], 1)
                self.assertEqual(self.driver.calls, [])
                self.assertIn(status, self.audit_rows()[-1]["reason"])

    def test_url_variants_of_one_person_dedupe(self):
        self.ledger["persons"] = {"in:jane": {"status": "invited", "last_ts": NOW}}
        person = cand("jane")
        person["profile_url"] = "https://www.linkedin.com/in/JANE?utm_source=x"
        summary = self.run_live([person])
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(summary["skipped"], 1)


class PacingAndWindow(BotCase):
    def test_delays_have_jitter_inside_the_configured_bounds(self):
        import random

        rng = random.Random(7)
        seen = {pace_delay(60.0, 180.0, rng) for _ in range(50)}
        self.assertTrue(all(60.0 <= d <= 180.0 for d in seen))
        self.assertGreater(len(seen), 1, "no jitter means no human-like pacing")

    def test_bad_delay_window_is_rejected(self):
        import random

        with self.assertRaises(ValueError):
            pace_delay(10.0, 5.0, random.Random())

    def test_no_sleep_before_the_first_invite_only_between(self):
        sleeps = []
        summary = self.run_live([cand("a"), cand("b"), cand("c")], sleeper=sleeps.append)
        self.assertEqual(summary["sent"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_outside_the_window_nothing_starts(self):
        def exploding_factory():
            raise AssertionError("outside the window the browser must never start")

        summary = run_queue(
            [cand("a")], self.cfg, self.ledger, self.audit,
            live=True, cap=20, day=DAY, window=NEVER, note=None, limit=0,
            min_delay=0, max_delay=0, driver_factory=exploding_factory, now_iso=NOW,
        )
        self.assertEqual(summary["stopped"], "outside_window")
        self.assertEqual(summary["sent"], 0)
        rows = self.audit_rows()
        self.assertEqual(rows[-1]["decision"], "refuse")

    def test_overnight_windows_wrap_midnight(self):
        from datetime import datetime, timezone

        def at(hour, minute):
            return datetime(2026, 11, 1, hour, minute, tzinfo=timezone.utc)

        self.assertTrue(inside_window("22:00-06:00", at(23, 30)))
        self.assertTrue(inside_window("22:00-06:00", at(2, 0)))
        self.assertFalse(inside_window("22:00-06:00", at(12, 0)))


class Notes(BotCase):
    def test_note_over_300_chars_is_rejected_before_any_send(self):
        with self.assertRaises(ValueError):
            check_note("x" * 301)
        self.assertEqual(check_note("hello"), "hello")
        self.assertIsNone(check_note(None))
        self.assertIsNone(check_note("   "))

    def test_cli_rejects_an_overlong_note_file(self):
        note = self.tmp / "note.txt"
        note.write_text("x" * 301, encoding="utf-8")
        queue = self.tmp / "q.json"
        queue.write_text(json.dumps([cand("a")]), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOL), "run", "--candidates", str(queue),
             "--state-dir", str(self.state), "--window", ALWAYS, "--note-file", str(note)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("300", proc.stderr + proc.stdout)


class Secrets(BotCase):
    def test_scrub_redacts_credential_fragments(self):
        dirty = "login failed password: s3cret li_at=abc123 session_token: xyz"
        clean = scrub(dirty)
        self.assertNotIn("s3cret", clean)
        self.assertNotIn("abc123", clean)
        self.assertNotIn("xyz", clean)
        self.assertIn("***", clean)

    def test_audit_log_never_carries_raw_secrets(self):
        append_audit(self.audit, {"ts": NOW, "decision": "note", "detail": "cookie: steal-me"})
        text = self.audit.read_text(encoding="utf-8")
        self.assertNotIn("steal-me", text)

    def test_cli_takes_no_credential_flags(self):
        parser = build_parser()
        option_strings = set()

        def collect(p):
            for action in p._actions:
                option_strings.update(action.option_strings)
                if hasattr(action, "_name_parser_map"):
                    for sub_p in action._name_parser_map.values():
                        collect(sub_p)

        collect(parser)
        joined = " ".join(sorted(option_strings)).lower()
        for forbidden in ["--password", "--token", "--cookie", "--secret", "--username", "--session"]:
            self.assertNotIn(forbidden, joined)

    def test_state_paths_are_gitignored(self):
        for path in ["linkedin_connect/ledger.json", "linkedin_connect/audit.jsonl"]:
            with self.subTest(path):
                proc = subprocess.run(
                    ["git", "check-ignore", "-q", path],
                    capture_output=True, cwd=str(REPO),
                )
                self.assertEqual(proc.returncode, 0, f"{path} must be ignored (no invite history in git)")


class StatusAndPlan(BotCase):
    def test_status_reports_quota_math(self):
        self.ledger["sends"] = [f"{DAY}T08:00:00+05:30"] * 3
        self.ledger["persons"] = {"in:a": {"status": "invited"}}
        path = self.state / "ledger.json"
        save_ledger(path, self.ledger)
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--state-dir", str(self.state)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["sent_today"], 3)
        self.assertEqual(out["remaining_today"], 17)
        self.assertEqual(out["persons_by_status"], {"invited": 1})

    def test_status_refuses_a_cap_above_twenty(self):
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--state-dir", str(self.state), "--max-per-day", "50"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)

    def test_plan_scores_without_writing_state(self):
        queue = self.tmp / "q.json"
        queue.write_text(json.dumps([cand("a"), cand("s", headline="Sales", location="London")]),
                         encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOL), "plan", "--candidates", str(queue),
             "--state-dir", str(self.state)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual([r["decision"] for r in out["rows"]], ["invite", "skip"])
        self.assertFalse(self.state.exists(), "plan must not write any state")


class DiscoverySeeds(BotCase):
    def test_queries_combine_role_seeds_with_domain_terms_and_locations(self):
        queries = discovery_queries(self.cfg)
        first = queries[0]
        self.assertIn("recruiter", first["keywords"])
        self.assertIn("genai", first["keywords"])
        self.assertIn("India", first["keywords"])
        roles = [q["role"] for q in queries[:3]]
        self.assertEqual(roles, ["recruiter", "hiring manager", "engineering manager"],
                         "the first searches must span roles, not exhaust one seed")

    def test_seeds_are_config_data(self):
        cfg = dict(self.cfg, discovery={"role_seeds": ["zyxq-plugh"], "locations": ["Nowhere"]})
        queries = discovery_queries(cfg)
        self.assertTrue(all("zyxq-plugh" in q["keywords"] for q in queries))
        self.assertFalse(discovery_queries(self.cfg) == queries)

    def test_no_seeds_means_no_queries(self):
        cfg = dict(self.cfg, discovery={"role_seeds": [], "locations": ["India"]})
        self.assertEqual(discovery_queries(cfg), [])


class DiscoveryRun(BotCase):
    def discover(self, queries, **over):
        kw = dict(
            live=True, day=DAY, window=ALWAYS, max_searches=3, max_pages=2,
            max_profile_views=5, max_queued=20, search_min_delay=0,
            search_max_delay=0, now_iso=NOW,
        )
        kw.update(over)
        driver = kw.pop("driver", None)
        if driver is None:
            driver = FakeDriver(searches=kw.pop("searches", []), views=kw.pop("views", []))
            self.driver = driver
        return run_discover(queries, self.cfg, self.ledger, self.cache, self.audit,
                            driver_factory=lambda: driver, **kw)

    def setUp(self):
        super().setUp()
        self.cache = {"version": 1, "pages": {}}

    def test_matching_cards_are_queued_with_reasons(self):
        summary = self.discover(
            [seed_query()], searches=[[search_card("a"), search_card("b", headline="Sales", location="London")]],
        )
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(summary["skipped"], 1)
        person = self.ledger["persons"]["in:a"]
        self.assertEqual(person["status"], "queued")
        self.assertIn("linkedin.com/in/a", person["profile_url"])
        rows = self.audit_rows()
        self.assertEqual(rows[-2]["decision"], "queue")
        self.assertIn("role:recruiter", rows[-2]["reason"])

    def test_budgets_bind_searches_pages_and_views(self):
        pages = [[search_card(f"p{i}")] for i in range(4)]
        driver = FakeDriver(searches=list(pages))
        summary = self.discover([seed_query(), seed_query("hiring manager genai India")],
                                driver=driver, max_searches=1, max_pages=2)
        self.assertEqual(len(driver.search_calls), 2, "one query, two pages, then stop")
        self.assertEqual(summary["deferred"], 1)

        self.setUp()  # fresh ledger+cache: part 1's fetched pages must not satisfy this part
        driver = FakeDriver(searches=list(pages))
        summary = self.discover([seed_query()], driver=driver, max_searches=1, max_pages=1)
        self.assertEqual(len(driver.search_calls), 1, "page budget binds even with results left")

        self.setUp()
        thin = [search_card("v1", headline=""), search_card("v2", headline=""), search_card("v3", headline="")]
        driver = FakeDriver(searches=[thin], views=[{"headline": "Tech Recruiter"}])
        summary = self.discover([seed_query()], driver=driver, max_profile_views=1)
        self.assertEqual(len(driver.view_calls), 1, "profile-view budget binds")
        self.assertEqual(summary["profile_views"], 1)

    def test_empty_page_stops_pagination_without_spending_budget(self):
        driver = FakeDriver(searches=[[search_card("a")], []])
        summary = self.discover([seed_query()], driver=driver, max_pages=5)
        self.assertEqual(len(driver.search_calls), 2)
        self.assertEqual(summary["queued"], 1)

    def test_search_fetches_are_paced(self):
        sleeps = []
        self.discover([seed_query(), seed_query("x")], max_pages=1,
                      searches=[[search_card("a")], [search_card("b")]], sleeper=sleeps.append)
        self.assertEqual(len(sleeps), 1, "one pause between the two fetches, none before the first")

    def test_queue_cap_matches_the_invite_cap(self):
        cards = [[search_card(f"q{i}")] for i in range(3)]
        driver = FakeDriver(searches=cards)
        summary = self.discover([seed_query()], driver=driver, max_pages=3, max_queued=2)
        self.assertEqual(summary["queued"], 2)
        self.assertEqual(summary["stopped"], "queue_cap_reached")
        self.assertEqual(queued_today(self.ledger, DAY), 2)

    def test_queued_and_terminal_persons_are_never_requeued(self):
        for status in sorted(DISCOVERY_STATUSES):
            with self.subTest(status=status):
                self.setUp()
                self.ledger["persons"] = {"in:known": {"status": status, "last_ts": NOW}}
                summary = self.discover([seed_query()], searches=[[search_card("known")]])
                self.assertEqual(summary["queued"], 0)
                self.assertEqual(summary["skipped"], 1)
                self.assertIn(status, self.audit_rows()[-1]["reason"])

    def test_stop_aborts_with_no_retry(self):
        signals = [LoginChallenge("wall"), CaptchaChallenge("c"), VerificationPrompt("v"),
                   RateLimited("l"), UnexpectedUI("u")]
        for signal in signals:
            with self.subTest(signal=signal.signal):
                self.setUp()
                with self.assertRaises(InviteStop):
                    self.discover([seed_query(), seed_query("x")],
                                  searches=[[search_card("a")], signal, [search_card("b")]])
                self.assertEqual(len(self.driver.search_calls), 2)
                self.assertTrue(self.driver.closed)
                self.assertEqual(self.audit_rows()[-1]["decision"], "stop")

    def test_outside_the_window_nothing_starts(self):
        def exploding():
            raise AssertionError("discovery outside the window must never start a browser")

        summary = run_discover(
            [seed_query()], self.cfg, self.ledger, self.cache, self.audit,
            live=True, day=DAY, window=NEVER, max_searches=3, max_pages=2,
            max_profile_views=5, max_queued=20, search_min_delay=0,
            search_max_delay=0, driver_factory=exploding, now_iso=NOW,
        )
        self.assertEqual(summary["stopped"], "outside_window")
        self.assertEqual(summary["searches_executed"], 0)


class DiscoveryDryRun(BotCase):
    def test_dry_run_touches_nothing(self):
        cache = {"version": 1, "pages": {
            cache_key("recruiter genai India", "India", 1):
                {"ts": NOW, "results": [search_card("cached")]},
        }}

        def exploding():
            raise AssertionError("dry-run discovery must not build a driver")

        summary = run_discover(
            [seed_query()], self.cfg, self.ledger, cache, self.audit,
            live=False, day=DAY, window=ALWAYS, max_searches=3, max_pages=2,
            max_profile_views=5, max_queued=20, search_min_delay=0,
            search_max_delay=0, driver_factory=exploding, now_iso=NOW,
        )
        kinds = [r["decision"] for r in summary["decisions"]]
        self.assertIn("would-queue", kinds, "cached matches are scored and reported")
        self.assertIn("would-search", kinds, "uncached pages are reported, never fetched")
        self.assertEqual(self.ledger, {"persons": {}, "sends": []})
        self.assertFalse(self.audit.is_file(), "dry-run discovery writes no audit entries")
        self.assertNotIn(
            cache_key("recruiter genai India", "India", 2), cache["pages"],
            "dry-run discovery writes no cache entries either",
        )

    def test_cli_dry_run_writes_no_state_files(self):
        targets = mini_targets(self.tmp)
        proc = subprocess.run(
            [sys.executable, str(TOOL), "discover", "--targets", str(targets),
             "--state-dir", str(self.state), "--window", ALWAYS],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WOULD SEARCH", proc.stdout)
        self.assertFalse(self.state.exists(), "dry-run discovery must not create any state")


class DiscoveryCache(BotCase):
    def test_rerun_never_rescrapes(self):
        queries = [seed_query()]
        cache: dict = {"version": 1, "pages": {}}
        first_driver = FakeDriver(searches=[[search_card("a")]])
        first = run_discover(
            queries, self.cfg, self.ledger, cache, self.audit,
            live=True, day=DAY, window=ALWAYS, max_searches=1, max_pages=1,
            max_profile_views=0, max_queued=20, search_min_delay=0,
            search_max_delay=0, driver_factory=lambda: first_driver, now_iso=NOW,
        )
        self.assertEqual(first["searches_executed"], 1)
        cache_path = self.state / "cache.json"
        save_cache(cache_path, cache)
        reloaded = load_cache(cache_path)
        ledger2 = {"persons": {}, "sends": []}
        tripwire = FakeDriver(searches=[AssertionError("a cached page must never be re-scraped")])
        second = run_discover(
            queries, self.cfg, ledger2, reloaded, self.audit,
            live=True, day=DAY, window=ALWAYS, max_searches=1, max_pages=1,
            max_profile_views=0, max_queued=20, search_min_delay=0,
            search_max_delay=0, driver_factory=lambda: tripwire, now_iso=NOW,
        )
        self.assertEqual(tripwire.search_calls, [])
        self.assertEqual(second["searches_executed"], 0)
        self.assertEqual(second["cache_hits"], 1)
        self.assertEqual(second["queued"], 1, "cached cards still score and queue")


class DiscoveryCli(BotCase):
    def test_live_discovers_and_queues_from_config_seeds(self):
        from tools import linkedin_connect as mod

        targets = mini_targets(self.tmp, discovery={"max_pages": 1})
        driver = FakeDriver(searches=[[search_card("a")], [search_card("b")]])
        real_driver = mod.PlaywrightDriver
        mod.PlaywrightDriver = lambda profile_dir: driver
        try:
            args = build_parser().parse_args([
                "discover", "--targets", str(targets), "--state-dir", str(self.state),
                "--window", ALWAYS, "--live",
            ])
            rc = cmd_discover(args)
        finally:
            mod.PlaywrightDriver = real_driver
        self.assertEqual(rc, 0)
        saved = load_ledger(self.state / "ledger.json")
        self.assertEqual(saved["persons"]["in:a"]["status"], "queued")
        self.assertEqual(len(driver.search_calls), 2, "both seed queries run within budget")

    def test_cli_stop_saves_state_and_exits_2(self):
        import io
        from contextlib import redirect_stderr

        from tools import linkedin_connect as mod

        targets = mini_targets(self.tmp)
        real_driver = mod.PlaywrightDriver
        mod.PlaywrightDriver = lambda profile_dir: FakeDriver(searches=[RateLimited("slow down")])
        try:
            args = build_parser().parse_args([
                "discover", "--targets", str(targets), "--state-dir", str(self.state),
                "--window", ALWAYS, "--live",
            ])
            err = io.StringIO()
            with redirect_stderr(err):
                rc = cmd_discover(args)
        finally:
            mod.PlaywrightDriver = real_driver
        self.assertEqual(rc, 2)
        self.assertIn("BOT STOPPED", err.getvalue())
        saved_cache = load_cache(self.state / "discovery_cache.json")
        self.assertIn("pages", saved_cache, "a stopped run still persists its cache")

    def test_max_queued_above_twenty_is_refused(self):
        targets = mini_targets(self.tmp)
        proc = subprocess.run(
            [sys.executable, str(TOOL), "discover", "--targets", str(targets),
             "--state-dir", str(self.state), "--window", ALWAYS, "--max-queued", "21"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)


class LedgerHandoff(BotCase):
    def test_run_from_ledger_invites_discover_queued_profiles(self):
        import io
        from contextlib import redirect_stdout

        ledger = {
            "persons": {
                "in:a": {"status": "queued", "last_ts": NOW,
                         "profile_url": "https://www.linkedin.com/in/a/",
                         "name": "a", "headline": "Tech Recruiter, GenAI", "location": "Remote"},
                "in:old": {"status": "invited", "last_ts": NOW,
                           "name": "old",
                           "profile_url": "https://www.linkedin.com/in/old/"},
            },
            "sends": [],
        }
        save_ledger(self.state / "ledger.json", ledger)
        args = build_parser().parse_args([
            "run", "--from-ledger", "--state-dir", str(self.state), "--window", ALWAYS,
        ])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cmd_run(args)
        self.assertEqual(rc, 0)
        self.assertIn("WOULD SEND", out.getvalue())
        self.assertIn("in:a", out.getvalue(), "the queued profile is offered")
        self.assertNotIn("in:old", out.getvalue(), "the already-invited profile is not re-offered")


if __name__ == "__main__":
    unittest.main()

