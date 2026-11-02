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
    DIALOG_MISSED,
    DISCOVERY_STATUSES,
    ENTRY_DIRECT,
    ENTRY_MORE,
    ENTRY_TIERS,
    ENTRY_UNKNOWN,
    FOLLOW_ONLY,
    MENU_MISSED,
    HARD_WEEKLY_CAP,
    MAX_CONSECUTIVE_UNKNOWN,
    NOTE_REQUIRED,
    RUN_SKIP_STATUSES,
    TERMINAL_STATUSES,
    UNKNOWN_CONTROLS,
    CaptchaChallenge,
    InviteDriver,
    InviteStop,
    LoginChallenge,
    MORE_MENU_READ_JS,
    PlaywrightDriver,
    RateLimited,
    UnexpectedUI,
    VerificationPrompt,
    append_audit,
    build_parser,
    cache_key,
    canonical_key,
    check_note,
    classify_interstitial,
    normalize_button_name,
    pick_invite_entry,
    scan_entry_controls,
    verify_dialog_target,
    cmd_discover,
    cmd_run,
    discovery_defaults,
    discovery_queries,
    effective_cap,
    entry_variant,
    inside_window,
    load_cache,
    load_ledger,
    pace_delay,
    parse_search_card,
    queue_tier,
    queued_today,
    run_discover,
    run_queue,
    save_cache,
    save_ledger,
    score_candidate,
    scrub,
    sends_last_7d,
    sends_last_days,
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


class WeeklyCap(BotCase):
    def test_rolling_window_counts_day_minus_six_not_day_minus_seven(self):
        self.ledger["sends"] = [
            "2026-10-26T10:00:00+05:30",  # DAY-6: in window
            "2026-10-25T10:00:00+05:30",  # DAY-7: out of window
        ]
        self.assertEqual(sends_last_7d(self.ledger, DAY), 1)

    def test_window_smaller_than_one_day_is_refused(self):
        with self.assertRaises(ValueError):
            sends_last_days(self.ledger, DAY, 0)

    def test_ninety_sends_in_window_refuses_the_pass_before_any_driver(self):
        self.ledger["sends"] = ["2026-10-27T09:00:00+05:30"] * HARD_WEEKLY_CAP
        started = []

        def factory():
            started.append(True)
            return FakeDriver(["sent"])

        summary = self.run_live([cand("late")], factory=factory)
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(summary["stopped"], "weekly_cap_reached")
        self.assertEqual(started, [], "a refused pass must not create a driver")
        self.assertEqual(self.audit_rows()[-1]["decision"], "refuse")

    def test_pass_halts_mid_loop_when_the_ninetieth_send_lands(self):
        self.ledger["sends"] = ["2026-10-27T09:00:00+05:30"] * (HARD_WEEKLY_CAP - 1)
        summary = self.run_live([cand(f"p{i}") for i in range(4)], cap=20)
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["stopped"], "weekly_cap_reached")
        self.assertEqual(len(self.driver.calls), 1)

    def test_eighty_nine_sends_still_allow_a_full_daily_pass(self):
        self.ledger["sends"] = ["2026-10-27T09:00:00+05:30"] * 70
        summary = self.run_live([cand("a"), cand("b")])
        self.assertEqual(summary["sent"], 2)
        self.assertIsNone(summary["stopped"])


class QueueOrder(BotCase):
    def tiered(self, slug, entry):
        candidate = cand(slug)
        candidate["entry"] = entry
        return candidate

    def test_explicit_entry_wins_over_status(self):
        self.assertEqual(entry_variant({"entry": ENTRY_DIRECT, "status": "queued"}), ENTRY_DIRECT)
        self.assertEqual(entry_variant({"entry": "bogus"}), ENTRY_UNKNOWN)
        self.assertEqual(entry_variant({}), ENTRY_UNKNOWN)
        self.assertEqual(entry_variant(None), ENTRY_UNKNOWN)

    def test_prior_follow_only_and_note_required_read_as_more(self):
        self.assertEqual(entry_variant({"status": FOLLOW_ONLY}), ENTRY_MORE)
        self.assertEqual(entry_variant({"status": NOTE_REQUIRED}), ENTRY_MORE)
        self.assertEqual(entry_variant({"status": "queued"}), ENTRY_UNKNOWN)

    def test_tiers_rank_direct_then_more_then_unknown(self):
        ledger = {"persons": {"in:m": {"status": FOLLOW_ONLY}}, "sends": []}
        self.assertLess(queue_tier(self.tiered("d", ENTRY_DIRECT), ledger),
                        queue_tier(self.tiered("m", ENTRY_UNKNOWN), ledger))
        self.assertLess(queue_tier(self.tiered("m", ENTRY_UNKNOWN), ledger),
                        queue_tier(self.tiered("u", ENTRY_UNKNOWN), ledger))
        self.assertEqual(queue_tier({"profile_url": ""}, ledger), ENTRY_TIERS[ENTRY_UNKNOWN])

    def test_run_sends_in_tier_order_with_stable_fifo_within_tier(self):
        submitted = [
            self.tiered("u1", ENTRY_UNKNOWN),
            self.tiered("d", ENTRY_DIRECT),
            self.tiered("m", ENTRY_MORE),
            self.tiered("u2", ENTRY_UNKNOWN),
        ]
        summary = self.run_live(submitted, script=["sent"] * 4)
        self.assertEqual(summary["sent"], 4)
        sent_urls = [c[0]["profile_url"] for c in self.driver.calls]
        self.assertEqual(sent_urls, [f"https://www.linkedin.com/in/{s}/" for s in ("d", "m", "u1", "u2")])
        tiers = [r["tier"] for r in summary["decisions"]]
        self.assertEqual(tiers, [ENTRY_DIRECT, ENTRY_MORE, ENTRY_UNKNOWN, ENTRY_UNKNOWN])


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
        mod.PlaywrightDriver = lambda profile_dir, **kw: FakeDriver([RateLimited("weekly limit")])
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
        from datetime import datetime

        today = datetime.now().astimezone().date().isoformat()
        self.ledger["sends"] = [f"{today}T08:00:00+05:30"] * 3
        self.ledger["persons"] = {"in:a": {"status": "invited"}}
        path = self.state / "ledger.json"
        save_ledger(path, self.ledger)
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--state-dir", str(self.state)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["day"], today)
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


class ParseSearchCard(unittest.TestCase):
    """parse_search_card fixtures are transcribed from the live people-search
    page captured during diagnosis (obfuscated b6bayf-style classes, hence
    the structure-based extraction). Unknown LinkedIn markup must parse to
    fields the scorer can use - or to empty fields that earn a profile view,
    never to shifted garbage."""

    PLACES = ["india", "remote", "bengaluru", "pune", "noida", "delhi",
              "maharashtra", "karnataka", "uttar pradesh", "haryana", "gurugram"]

    def test_full_card_parses_name_headline_location(self):
        card = parse_search_card(
            "https://www.linkedin.com/in/lazar-selvaras/",
            "Lazar Selvaras • 2nd",
            ["Lazar Selvaras • 2nd",
             "TA Agile Strategist, Career Enabler and Recruiting Evangelist, General Manager - TA & Strategy, Swiggy",
             "Bengaluru, Karnataka, India",
             "Follow",
             "General Manager TA - Tech/Leadership Hiring at Swiggy since April 2025."],
            self.PLACES,
        )
        self.assertEqual(card["name"], "Lazar Selvaras")
        self.assertTrue(card["headline"].startswith("TA Agile Strategist"))
        self.assertEqual(card["location"], "Bengaluru, Karnataka, India")

    def test_degree_badge_never_leaks_into_fields(self):
        card = parse_search_card(
            "https://www.linkedin.com/in/x/", "Ankita Yajnik • 2nd",
            ["Ankita Yajnik • 2nd", "• 2nd",
             "Senior Specialist, Technical Recruiter - India, South Asia @ Mastercard",
             "Pune District, Maharashtra, India", "Follow"],
            self.PLACES,
        )
        self.assertEqual(card["name"], "Ankita Yajnik")
        self.assertNotIn("•", card["headline"])
        self.assertEqual(card["location"], "Pune District, Maharashtra, India")

    def test_thin_card_leaves_headline_empty_for_enrichment(self):
        card = parse_search_card(
            "https://www.linkedin.com/in/y/", "Y",
            ["Y", "Noida, Uttar Pradesh, India", "Follow"],
            self.PLACES,
        )
        self.assertEqual(card["headline"], "", "a location in the headline slot must not pose as one")
        self.assertEqual(card["location"], "Noida, Uttar Pradesh, India")

    def test_non_place_third_line_means_no_location(self):
        card = parse_search_card(
            "https://www.linkedin.com/in/z/", "Z",
            ["Z", "Sales Executive, FMCG", "Follow"],
            self.PLACES,
        )
        self.assertEqual(card["headline"], "Sales Executive, FMCG")
        self.assertEqual(card["location"], "")

    def test_location_outside_the_lens_is_not_claimed(self):
        card = parse_search_card(
            "https://www.linkedin.com/in/w/", "W",
            ["W", "Tech Recruiter", "London, United Kingdom", "Follow"],
            self.PLACES,
        )
        self.assertEqual(card["headline"], "Tech Recruiter")
        self.assertEqual(card["location"], "", "outside-lens places stay empty rather than guessed")

    def test_extraction_query_uses_stable_hooks_not_classes(self):
        from tools.linkedin_connect import SEARCH_CARDS_JS

        self.assertIn('a[href*="/in/"]', SEARCH_CARDS_JS)
        self.assertNotIn("reusable-search", SEARCH_CARDS_JS)
        self.assertNotIn("entity-result", SEARCH_CARDS_JS)


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

    def test_view_observed_entry_persists_to_the_queued_person(self):
        thin = [search_card("v", headline="")]
        driver = FakeDriver(
            searches=[thin],
            views=[{"headline": "Tech Recruiter", "location": "India", "entry": ENTRY_DIRECT}],
        )
        summary = self.discover([seed_query()], driver=driver, max_profile_views=1)
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(self.ledger["persons"]["in:v"].get("entry"), ENTRY_DIRECT)

    def test_view_without_entry_leaves_the_queued_person_untiered(self):
        thin = [search_card("w", headline="")]
        driver = FakeDriver(
            searches=[thin],
            views=[{"headline": "Tech Recruiter", "location": "India"}],
        )
        summary = self.discover([seed_query()], driver=driver, max_profile_views=1)
        self.assertEqual(summary["queued"], 1)
        self.assertNotIn("entry", self.ledger["persons"]["in:w"])

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


class QueueCapAccounting(BotCase):
    def test_queue_log_counts_cumulatively_despite_resolution(self):
        self.ledger["queued_log"] = [f"{DAY}T08:00:00+05:30", f"{DAY}T09:00:00+05:30"]
        self.ledger["persons"] = {
            "in:sent": {"status": "invited", "last_ts": NOW},
            "in:q": {"status": "queued", "last_ts": NOW},
        }
        self.assertEqual(queued_today(self.ledger, DAY), 2,
                         "resolving a profile must not free its discovery slot")

    def test_legacy_ledger_falls_back_to_still_queued(self):
        self.assertNotIn("queued_log", self.ledger)
        self.ledger["persons"] = {
            "in:q": {"status": "queued", "last_ts": NOW},
            "in:old": {"status": "invited", "last_ts": NOW},
        }
        self.assertEqual(queued_today(self.ledger, DAY), 1)

    def test_queue_event_appends_to_log(self):
        cache = {"version": 1, "pages": {}}
        summary = run_discover(
            [seed_query()], self.cfg, self.ledger, cache, self.audit,
            live=True, day=DAY, window=ALWAYS, max_searches=1, max_pages=1,
            max_profile_views=0, max_queued=20, search_min_delay=0,
            search_max_delay=0,
            driver_factory=lambda: FakeDriver(searches=[[search_card("a")]]),
            now_iso=NOW,
        )
        self.assertEqual(summary["queued"], 1)
        self.assertEqual(self.ledger["queued_log"], [NOW])


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
        mod.PlaywrightDriver = lambda profile_dir, **kw: driver
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
        mod.PlaywrightDriver = lambda profile_dir, **kw: FakeDriver(searches=[RateLimited("slow down")])
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


class InviteEntry(BotCase):
    def test_normalize_strips_icon_glyphs_but_never_collapses(self):
        self.assertEqual(normalize_button_name("+ Follow"), "Follow")
        self.assertEqual(normalize_button_name("  Connect  "), "Connect")
        self.assertEqual(normalize_button_name("Invite Arnab Banerjee to connect"),
                         "Invite Arnab Banerjee to connect")
        self.assertEqual(normalize_button_name(""), "")
        self.assertEqual(normalize_button_name(None), "")

    def test_scan_partitions_page_controls(self):
        scan = scan_entry_controls(["Home", "Me", "More", "Follow Arnab Banerjee", "Message"])
        self.assertEqual(scan["connect"], [])
        self.assertTrue(scan["follow"], "named Follow variants count, icon glyphs included")
        self.assertEqual(scan["more"], [2])

    def test_scan_accepts_bare_and_named_connect(self):
        scan = scan_entry_controls(["Connect", "Connect Arnab Banerjee", "Message"])
        self.assertEqual(scan["connect"], [0, 1], "clicks are dialog-verified, so both qualify")

    def test_scan_rejects_decoys_and_caps_indices(self):
        scan = scan_entry_controls(["Connections", "Invite X to connect", "Open control menu",
                                    "Connect", "Connect", "Connect", "Connect"])
        self.assertEqual(scan["connect"], [3, 4, 5], "at most 3 click candidates, decoys excluded")
        self.assertFalse(scan["follow"])
        self.assertEqual(scan["more"], [])

    def test_titled_more_counts_as_more(self):
        scan = scan_entry_controls(["More options", "Message"])
        self.assertEqual(scan["more"], [0], "title-attribute names must resolve, not vanish")
        self.assertEqual(scan["connect"], [])
        self.assertFalse(scan["follow"])

    def test_follow_only_skips_with_reason_and_consumes_no_cap(self):
        summary = self.run_live([cand("f"), cand("s")], script=[FOLLOW_ONLY, "sent"])
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.ledger["persons"]["in:f"]["status"], FOLLOW_ONLY)
        self.assertEqual(len(self.ledger["sends"]), 1, "the skip consumed no cap")
        reasons = [r["reason"] for r in summary["decisions"] if r["decision"] == "skip"]
        self.assertTrue(any("follow-only" in reason for reason in reasons))

    def test_follow_only_is_never_requeued(self):
        self.ledger["persons"] = {"in:f": {"status": FOLLOW_ONLY, "last_ts": NOW}}
        self.assertIn(FOLLOW_ONLY, RUN_SKIP_STATUSES)
        self.assertIn(FOLLOW_ONLY, DISCOVERY_STATUSES)
        summary = self.run_live([cand("f")])
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.driver.calls, [])


class CapturedPageScan(unittest.TestCase):
    """Scan tests against the captured live button dump of a real
    Follow-primary profile (transcribed in-test because the diag dir itself
    is gitignored runtime state). The dump carries no Connect string at all,
    four named Follow variants, and rail noise."""

    CAPTURED_BUTTONS = [
        "Skip to search", "Skip to main content", "Skip to primary content",
        "Skip to aside", "Skip to footer", "Close jump menu",
        "Home, 1 new notification", "Me", "For Business", "More",
        "Follow Arnab Banerjee", "Follow Arnab Banerjee", "More",
        "Follow Arnab Banerjee", "More", "… more", "Next",
        "Follow Arnab Banerjee", "Open control menu for post by Arnab Banerjee",
    ]

    def test_captured_page_scans_follow_with_mores(self):
        scan = scan_entry_controls(self.CAPTURED_BUTTONS)
        self.assertEqual(scan["connect"], [], "no Connect string anywhere on the page")
        self.assertTrue(scan["follow"])
        self.assertEqual(scan["more"], [9, 12, 14])

    def test_captured_page_never_resolves_to_a_click(self):
        scan = scan_entry_controls(self.CAPTURED_BUTTONS)
        self.assertEqual(scan["connect"], [])


class VerifyDialogTarget(unittest.TestCase):
    def test_full_name_match_verifies(self):
        self.assertTrue(verify_dialog_target("Send invitation to Arnab Banerjee", "Arnab Banerjee"))

    def test_match_ignores_case_and_spacing(self):
        self.assertTrue(verify_dialog_target("  invite   ARNAB banerjee  ", "arnab  Banerjee"))

    def test_wrong_person_fails(self):
        self.assertFalse(verify_dialog_target("Send invitation to Mohan Kumar", "Arnab Banerjee"))

    def test_empty_dialog_or_name_fails(self):
        self.assertFalse(verify_dialog_target("", "Arnab Banerjee"))
        self.assertFalse(verify_dialog_target("Send invitation", ""))
        self.assertFalse(verify_dialog_target("Send invitation", None))


class MoreConnectEntry(unittest.TestCase):
    def test_named_entry_is_clickable_verbatim(self):
        names = ["Save to PDF", "Invite Arnab Banerjee to connect", "Report / Block"]
        self.assertEqual(pick_invite_entry(names, "Arnab Banerjee"),
                         "Invite Arnab Banerjee to connect")

    def test_match_is_case_insensitive(self):
        self.assertEqual(pick_invite_entry(["INVITE ARNAB BANERJEE TO CONNECT"], "arnab banerjee"),
                         "INVITE ARNAB BANERJEE TO CONNECT")

    def test_bare_connect_is_never_enough(self):
        self.assertIsNone(pick_invite_entry(["Connect", "Message"], "Arnab Banerjee"),
                          "a bare Connect could belong to a suggested profile - never click it")

    def test_menu_bare_connect_accepted_when_unique(self):
        menu = ["Send profile in a message", "Save to PDF", "Connect",
                "Report Arnab", "Block Arnab", "About this member"]
        self.assertEqual(pick_invite_entry(menu, "Arnab Banerjee", bare_ok=True), "Connect")

    def test_menu_bare_connect_rejected_when_ambiguous(self):
        menu = ["Connect", "Connect", "Save to PDF"]
        self.assertIsNone(pick_invite_entry(menu, "Arnab Banerjee", bare_ok=True),
                          "two bare Connects in one menu must fail closed")

    def test_named_entry_still_wins_over_bare(self):
        menu = ["Connect", "Invite Arnab Banerjee to connect"]
        self.assertEqual(pick_invite_entry(menu, "Arnab Banerjee", bare_ok=True),
                         "Invite Arnab Banerjee to connect")

    def test_other_persons_entry_is_rejected(self):
        self.assertIsNone(pick_invite_entry(["Invite Mohan Kumar to connect"], "Arnab Banerjee"))

    def test_empty_or_nameless_never_matches(self):
        self.assertIsNone(pick_invite_entry([], "Arnab Banerjee"))
        self.assertIsNone(pick_invite_entry(["Invite Arnab Banerjee to connect"], ""))
        self.assertIsNone(pick_invite_entry(["Invite Arnab Banerjee to connect"], None))


class MoreMenuLiRead(unittest.TestCase):
    """Regression for the 2026-11-02 live miss: LinkedIn renders More-menu
    items as plain <li> under [role="menu"] (no menuitem role, no inner
    button/anchor), so a menu read without the li clause sees only
    surrounding dialog chrome and a real Connect is misreported as a
    follow-only profile. Observed live: a probe read with the li clause
    listed a unique bare Connect, while the run path without it skipped."""

    DIALOG_CHROME = ["Reset", "Done", "Close Modal Dialog"]
    MENU_LI_ITEMS = ["Send profile in a message", "Save to PDF", "Connect",
                     "Report Arnab", "Block Arnab", "About this member"]

    class FakeClick:
        def __init__(self, log, label):
            self.log = log
            self.label = label

        def click(self, timeout=None):
            self.log.append(self.label)

    class FakeRole:
        def __init__(self, log, label):
            self.log = log
            self.label = label

        def nth(self, index):
            return MoreMenuLiRead.FakeClick(self.log, f"{self.label}[{index}]")

    class FakeText:
        def __init__(self, log, text):
            self.log = log
            self.text = text

        @property
        def first(self):
            return MoreMenuLiRead.FakeClick(self.log, self.text)

    class FakeLocator:
        def __init__(self, log):
            self.log = log

        def get_by_text(self, text, exact=False):
            return MoreMenuLiRead.FakeText(self.log, text)

    class FakeKeyboard:
        def press(self, key):
            pass

    class FakePage:
        """Emulates the observed DOM: menu items live in li elements, so
        evaluate() surfaces them only when the selector queries li."""

        def __init__(self):
            self.read_selectors = []
            self.clicked = []
            self.keyboard = MoreMenuLiRead.FakeKeyboard()

        def get_by_role(self, role):
            return MoreMenuLiRead.FakeRole(self.clicked, role)

        def wait_for_timeout(self, ms):
            pass

        def evaluate(self, script):
            self.read_selectors.append(script)
            items = list(MoreMenuLiRead.DIALOG_CHROME)
            if '[role="menu"] li' in script:
                items += MoreMenuLiRead.MENU_LI_ITEMS
            return items

        def locator(self, selector):
            return MoreMenuLiRead.FakeLocator(self.clicked)

    def test_li_menu_items_reach_the_entry_picker(self):
        self.assertIn('[role="menu"] li', MORE_MENU_READ_JS,
                      "the run-path menu read must query li items")
        page = self.FakePage()
        driver = PlaywrightDriver(Path("/tmp/lc-test-profile"))
        driver._page_state = lambda page: None
        driver._dialog_targets = lambda page, candidate: True
        sent_notes = []
        driver._send_bare_from_dialog = lambda page, note: (sent_notes.append(note), "sent")[1]
        result = PlaywrightDriver._more_connect_flow(
            driver, page, [0],
            {"name": "Arnab Banerjee",
             "profile_url": "https://www.linkedin.com/in/arnab-banerjee-86aa6186/"},
            None,
        )
        self.assertEqual(result, "sent")
        self.assertIn("Connect", page.clicked,
                      f"the observed bare Connect must be clicked, clicked={page.clicked}")
        self.assertEqual(sent_notes, [None], "bare invite carries no note")


class StepVerify(unittest.TestCase):
    """Self-verifying More->Connect transitions: every UI step asserts
    before proceeding (menu non-empty + entry match; dialog target), each
    step gets ONE bounded re-attempt, and every attempt is captured win or
    lose. Step misses skip with the step reason - never a blind click."""

    CHROME = ["Reset", "Done", "Close Modal Dialog"]
    MENU = ["Send profile in a message", "Save to PDF", "Connect",
            "Report Arnab", "Block Arnab", "About this member"]
    CAND = {"name": "Arnab Banerjee",
            "profile_url": "https://www.linkedin.com/in/arnab-banerjee-86aa6186/"}

    class FakeClick:
        def __init__(self, log, label):
            self.log = log
            self.label = label

        def click(self, timeout=None):
            self.log.append(self.label)

    class FakePage:
        """Scripted reads (one item list per menu read) and scripted entry-
        click failures; More expands always land."""

        def __init__(self, reads, bad_clicks=0):
            self.reads = list(reads)
            self.bad_clicks = bad_clicks
            self.expands = 0
            self.entry_clicks = []
            self.escapes = 0

        def get_by_role(self, role):
            outer = self

            class Role:
                def nth(self, index):
                    outer.expands += 1
                    return StepVerify.FakeClick([], f"more[{index}]")

            return Role()

        def wait_for_timeout(self, ms):
            pass

        def evaluate(self, script):
            if len(self.reads) > 1:
                return self.reads.pop(0)
            return self.reads[0]

        def locator(self, selector):
            outer = self

            class Loc:
                def get_by_text(self, text, exact=False):
                    class First:
                        @property
                        def first(self):
                            outer2 = outer

                            class Click:
                                def click(self, timeout=None):
                                    if outer2.bad_clicks > 0:
                                        outer2.bad_clicks -= 1
                                        raise RuntimeError("click intercepted")
                                    outer2.entry_clicks.append(text)

                            return Click()

                    return First()

            return Loc()

        @property
        def keyboard(self):
            outer = self

            class Keys:
                def press(self, key):
                    outer.escapes += 1

            return Keys()

    def flow(self, page, dialog=True, note=None):
        driver = PlaywrightDriver(Path("/tmp/lc-test-profile"))
        driver._page_state = lambda pg: None
        verdicts = list(dialog) if isinstance(dialog, (list, tuple)) else [dialog]
        driver._dialog_targets = lambda pg, c: verdicts.pop(0) if len(verdicts) > 1 else verdicts[0]
        sent = []
        driver._send_bare_from_dialog = lambda pg, n: (sent.append(n), "sent")[1]
        result = PlaywrightDriver._more_connect_flow(driver, page, [0], dict(self.CAND), note)
        return result, sent

    def test_menu_miss_twice_returns_menu_missed(self):
        page = self.FakePage([self.CHROME])
        result, _ = self.flow(page)
        self.assertEqual(result, MENU_MISSED)
        self.assertEqual(page.expands, 2, "initial expand + ONE bounded re-expand")
        self.assertEqual(page.entry_clicks, [], "no entry click without a verified menu")

    def test_menu_recovers_on_reread_then_sends(self):
        page = self.FakePage([self.CHROME, self.MENU])
        result, sent = self.flow(page)
        self.assertEqual(result, "sent")
        self.assertEqual(page.entry_clicks, ["Connect"])
        self.assertEqual(sent, [None], "bare invite carries no note")

    def test_dialog_miss_twice_returns_dialog_missed(self):
        page = self.FakePage([self.MENU], )
        result, _ = self.flow(page, dialog=[False, False])
        self.assertEqual(result, DIALOG_MISSED)
        self.assertEqual(page.entry_clicks, ["Connect", "Connect"],
                          "ONE bounded re-press of the verified entry")

    def test_dialog_recovers_on_repress_then_sends(self):
        page = self.FakePage([self.MENU])
        result, sent = self.flow(page, dialog=[False, True])
        self.assertEqual(result, "sent")
        self.assertEqual(sent, [None])

    def test_entry_click_failure_still_misses_without_dialog(self):
        page = self.FakePage([self.MENU], bad_clicks=9)
        result, _ = self.flow(page, dialog=True)
        self.assertEqual(result, DIALOG_MISSED)
        self.assertEqual(page.entry_clicks, [], "intercepted clicks never reach the dialog")

    def test_step_misses_join_the_storm_streak_without_ledger_writes(self):
        ledger = {"persons": {}, "sends": []}
        from tempfile import TemporaryDirectory
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        audit_path = Path(tmp.name) / "audit.jsonl"
        summary = run_queue(
            [dict(self.CAND, profile_url=f"https://www.linkedin.com/in/p{i}/") for i in range(5)],
            {"min_score": 0}, ledger, audit_path,
            live=True, cap=20, day=DAY, window=ALWAYS, note=None, limit=0,
            min_delay=0, max_delay=0,
            driver_factory=lambda: FakeDriver([MENU_MISSED, DIALOG_MISSED] * 3),
            now_iso=NOW,
        )
        self.assertEqual(summary["stopped"], "unknown_controls")
        self.assertEqual(summary["unknown_skips"], 4)
        self.assertEqual(ledger["persons"], {})
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        reasons = [r["reason"] for r in rows]
        self.assertTrue(any("re-expanded once" in reason for reason in reasons))
        self.assertTrue(any("re-pressed once" in reason for reason in reasons))

    def test_capture_writes_screenshot_and_dom_snippet(self):
        from tempfile import TemporaryDirectory
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class ShotPage:
            def __init__(self):
                self.shots = []

            def screenshot(self, path):
                self.shots.append(path)
                Path(path).write_bytes(b"PNG")

            def evaluate(self, script):
                return "Arnab Banerjee profile DOM"

        driver = PlaywrightDriver(Path("/tmp/lc-test-profile"), capture_dir=Path(tmp.name))
        page = ShotPage()
        driver._capture(page, "in-arnab", "more0_menu", True)
        files = sorted(p.name for p in Path(tmp.name).iterdir())
        self.assertEqual(len(files), 2)
        self.assertTrue(any(name.endswith("_ok.png") for name in files))
        txt = next(p for p in Path(tmp.name).iterdir() if p.suffix == ".txt")
        self.assertIn("Arnab Banerjee", txt.read_text(encoding="utf-8"))

    def test_capture_failure_never_fails_the_run(self):
        from tempfile import TemporaryDirectory
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class BadPage:
            def screenshot(self, path):
                raise RuntimeError("no display")

            def evaluate(self, script):
                raise RuntimeError("gone")

        driver = PlaywrightDriver(Path("/tmp/lc-test-profile"), capture_dir=Path(tmp.name))
        driver._capture(BadPage(), "in-x", "step", False)  # must not raise

    def test_slug_is_filesystem_safe(self):
        self.assertEqual(PlaywrightDriver._slug(self.CAND), "in-arnab-banerjee-86aa6186")
        self.assertEqual(PlaywrightDriver._slug({"profile_url": "   "}), "unkeyable")
        self.assertEqual(PlaywrightDriver._slug({}), "unkeyable")


class NoteRequired(BotCase):
    def test_note_required_skips_and_persists_without_cap(self):
        summary = self.run_live([cand("n"), cand("s")], script=[NOTE_REQUIRED, "sent"])
        self.assertEqual(summary["sent"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(self.ledger["persons"]["in:n"]["status"], NOTE_REQUIRED)
        self.assertEqual(len(self.ledger["sends"]), 1)
        self.assertIn(NOTE_REQUIRED, RUN_SKIP_STATUSES)
        self.assertIn(NOTE_REQUIRED, DISCOVERY_STATUSES)
        reasons = [r["reason"] for r in summary["decisions"] if r.get("result") == NOTE_REQUIRED]
        self.assertTrue(any("no note is ever written" in reason for reason in reasons))


class UnknownStorm(BotCase):
    def test_fourth_consecutive_unknown_halts(self):
        self.assertEqual(MAX_CONSECUTIVE_UNKNOWN, 3)
        driver = FakeDriver(script=[UNKNOWN_CONTROLS] * 6)
        summary = self.run_live([cand(f"u{i}") for i in range(6)], factory=lambda: driver)
        self.assertEqual(summary["stopped"], "unknown_controls")
        self.assertEqual(len(driver.calls), 4, "the 4th consecutive unknown halts the run")
        self.assertEqual(summary["unknown_skips"], 4)
        self.assertEqual(summary["sent"], 0)
        self.assertNotIn("in:u0", self.ledger["persons"],
                         "unknown profiles stay queued - the ledger is never written over")

    def test_any_other_decision_resets_the_streak(self):
        driver = FakeDriver(script=[UNKNOWN_CONTROLS, "sent", UNKNOWN_CONTROLS,
                                    UNKNOWN_CONTROLS, UNKNOWN_CONTROLS, "sent"])
        summary = self.run_live([cand(f"u{i}") for i in range(6)], factory=lambda: driver)
        self.assertIsNone(summary["stopped"])
        self.assertEqual(summary["sent"], 2)
        self.assertEqual(len(driver.calls), 6)

    def test_cli_halt_is_loud_with_exit_2(self):
        import io
        from contextlib import redirect_stderr

        from tools import linkedin_connect as mod

        queue = self.tmp / "q.json"
        queue.write_text(json.dumps([cand(f"u{i}") for i in range(5)]), encoding="utf-8")
        real_driver = mod.PlaywrightDriver
        mod.PlaywrightDriver = lambda profile_dir, **kw: FakeDriver(script=[UNKNOWN_CONTROLS] * 5)
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
        self.assertIn("consecutive", err.getvalue())


class SnapshotPreserved(BotCase):
    def test_run_touch_never_strips_the_discovery_snapshot(self):
        """Fresh candidate fields win, but fields the candidate omits fall
        back to the stored snapshot instead of blanking it - otherwise a
        run that only learns a status silently drops the profile below
        threshold on every later pass."""
        self.ledger["persons"] = {
            "in:a": {"status": "queued", "last_ts": NOW,
                     "profile_url": "https://www.linkedin.com/in/a/",
                     "name": "a", "headline": "Tech Recruiter, GenAI", "location": "Remote"},
        }
        driver = FakeDriver([FOLLOW_ONLY])
        summary = self.run_live(
            [{"profile_url": "https://www.linkedin.com/in/a/", "name": "a",
              "headline": "Hiring Manager, GenAI"}],
            factory=lambda: driver,
        )
        stored = self.ledger["persons"]["in:a"]
        self.assertEqual(stored["status"], FOLLOW_ONLY)
        self.assertEqual(stored["headline"], "Hiring Manager, GenAI", "fresh input wins")
        self.assertEqual(stored["location"], "Remote", "omitted fields fall back, never blank")
        self.assertEqual(summary["skipped"], 1)


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

