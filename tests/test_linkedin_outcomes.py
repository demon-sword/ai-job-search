"""Non-sending regressions from the Nov-20 platform captures and interruption."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import linkedin_connect as bot

CANDIDATE = {"profile_url": "https://www.linkedin.com/in/rangansen/",
             "name": "Rangan Sen", "headline": "CTO GenAI", "location": "India"}
DAY = "2026-11-20"
NOW = DAY + "T01:32:07+05:30"
ROOT = Path(__file__).resolve().parent.parent


def snapshot(**overrides):
    result = {"profile_url": CANDIDATE["profile_url"], "name": "Rangan Sen",
              "selector": "main section", "pending": False, "first_degree": False,
              "receipts": []}
    result.update(overrides)
    return result


class ReceiptPage:
    def __init__(self, states):
        self.states = list(states)
        self.clicks = 0
        self.waits = []

    def evaluate(self, script, arg=None):
        return self.states[0]

    def wait_for_timeout(self, ms):
        self.waits.append(ms)
        if len(self.states) > 1:
            self.states.pop(0)

    def get_by_role(self, role, **kwargs):
        page = self

        class Control:
            @property
            def first(self):
                return self

            def count(self):
                return int(role == "button")

            def get_by_role(self, role, **kwargs):
                return page.get_by_role(role, **kwargs)

            def click(self, **kwargs):
                page.clicks += 1

        return Control()


class ReceiptRecognition(unittest.TestCase):
    def driver(self):
        driver = bot.PlaywrightDriver(Path("unused-scratch-profile"))
        driver._page_state = lambda page: None
        driver._capture = lambda *args, **kwargs: None
        driver._dialog_targets = lambda page, candidate: True
        return driver

    def test_modal_send_accepts_an_immediate_target_receipt(self):
        page = ReceiptPage([snapshot(pending=True)])
        self.assertEqual(self.driver()._send_bare_from_dialog(page, None, CANDIDATE), "sent")
        self.assertEqual(page.clicks, 1)

    def test_direct_connect_can_send_without_a_dialog_and_is_never_repressed(self):
        driver = self.driver()
        page = ReceiptPage([snapshot(pending=True, receipts=["Invitation sent to Rangan."])])
        presses = []
        driver._dialog_targets = lambda page, candidate: False
        result = driver._press_connect_and_send(
            page, lambda: presses.append("Connect"), CANDIDATE, None, "rangan", "direct")
        self.assertEqual(result, "sent")
        self.assertEqual(presses, ["Connect"])

    def test_modal_send_waits_for_a_delayed_target_receipt(self):
        page = ReceiptPage([snapshot(), snapshot(), snapshot(pending=True)])
        result = self.driver()._send_bare_from_dialog(page, None, CANDIDATE)
        self.assertEqual(result, "sent")
        self.assertEqual(page.clicks, 1)
        self.assertGreaterEqual(len(page.waits), 2)

    def test_modal_disappearance_without_acknowledgement_is_uncertain(self):
        page = ReceiptPage([snapshot()])
        with self.assertRaises(bot.UncertainSend):
            self.driver()._send_bare_from_dialog(page, None, CANDIDATE)
        self.assertEqual(page.clicks, 1)

    def test_suggested_person_or_wrong_profile_cannot_supply_confirmation(self):
        for state in [snapshot(name="Someone Else", pending=True),
                      snapshot(profile_url="https://www.linkedin.com/in/else/", pending=True),
                      snapshot(receipts=["Invitation sent to Someone Else."])]:
            with self.subTest(state=state):
                self.assertIsNone(bot.terminal_outcome(state, CANDIDATE, after_action=True))
        self.assertEqual(bot.terminal_outcome(snapshot(pending=True), CANDIDATE,
                                            after_action=False), "already_pending")

    def test_click_exception_is_uncertain_not_a_second_click(self):
        driver = self.driver()
        attempts = []

        def interrupted_click():
            attempts.append(1)
            raise RuntimeError("browser disconnected during click")

        with self.assertRaises(bot.UncertainSend):
            driver._press_connect_and_send(ReceiptPage([snapshot()]), interrupted_click,
                                          CANDIDATE, None, "rangan", "direct")
        self.assertEqual(attempts, [1])


class DurableAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.ledger = bot.blank_ledger()
        self.audit = self.state / bot.AUDIT_NAME
        self.cfg = {"min_score": 1, "role_signals": [{"match": "CTO", "weight": 3}]}
        self.enterContext(patch.object(bot, "inside_window", return_value=True))

    def run_queue(self, driver, candidates=None, **kwargs):
        return bot.run_queue(candidates or [dict(CANDIDATE)], self.cfg, self.ledger, self.audit,
                             live=True, cap=20, day=DAY, window="00:00-23:59", note=None,
                             limit=0, min_delay=60, max_delay=60, now_iso=NOW,
                             driver_factory=lambda: driver,
                             sleeper=kwargs.pop("sleeper", lambda seconds: None), **kwargs)

    def test_abrupt_process_exit_preserves_reservation_and_blocks_restart(self):
        code = '''
import os, pathlib
from tools import linkedin_connect as b
b.inside_window = lambda window: True
class Driver:
 def start(self): pass
 def session_is_live(self): return True
 def close(self): pass
 def send_invite(self, candidate, note): os._exit(9)
b.run_queue([%r], %r, b.blank_ledger(), pathlib.Path(%r), live=True, cap=20,
 day=%r, window="00:00-23:59", note=None, limit=0, min_delay=60, max_delay=60,
 now_iso=%r, driver_factory=Driver)
''' % (CANDIDATE, self.cfg, str(self.audit), DAY, NOW)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 9, result.stderr)
        self.ledger = bot.load_ledger(self.state / bot.LEDGER_NAME)
        self.assertEqual(bot.quota_today(self.ledger, DAY), 1)
        self.assertEqual(bot.sends_today(self.ledger, DAY), 0)
        called = []
        summary = bot.run_queue([dict(CANDIDATE)], self.cfg, self.ledger, self.audit,
                                live=True, cap=20, day=DAY, window="00:00-23:59", note=None,
                                limit=0, min_delay=60, max_delay=60,
                                driver_factory=lambda: called.append("browser"), now_iso=NOW)
        self.assertEqual(summary["stopped"], "reconciliation_required")
        self.assertEqual(called, [])
        out = io.StringIO()
        args = bot.build_parser().parse_args(["status", "--json", "--state-dir", str(self.state)])
        with patch.object(bot, "today_str", return_value=DAY), redirect_stdout(out):
            bot.cmd_status(args)
        status = json.loads(out.getvalue())
        self.assertEqual(status["reserved_today"], 1)
        self.assertEqual(status["remaining_today"], 19)
        self.assertEqual(status["last_run"]["status"], "interrupted")
        self.assertIn("rangansen", status["last_error"])

    def test_termination_after_ack_before_result_commit_still_reserves_quota(self):
        code = '''
import os, pathlib
from tools import linkedin_connect as b
b.inside_window = lambda window: True
save = b.save_ledger
def crash_after_ack(path, ledger):
 if getattr(b, \"acknowledged\", False): os._exit(11)
 save(path, ledger)
b.save_ledger = crash_after_ack
class Driver:
 def start(self): pass
 def session_is_live(self): return True
 def close(self): pass
 def send_invite(self, candidate, note):
  b.acknowledged = True
  return \"sent\"
b.run_queue([%r], %r, b.blank_ledger(), pathlib.Path(%r), live=True, cap=20,
 day=%r, window=\"00:00-23:59\", note=None, limit=0, min_delay=60, max_delay=60,
 now_iso=%r, driver_factory=Driver)
''' % (CANDIDATE, self.cfg, str(self.audit), DAY, NOW)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, 11, result.stderr)
        saved = bot.load_ledger(self.state / bot.LEDGER_NAME)
        self.assertEqual(saved["attempts"]["in:rangansen"]["status"], "attempting")
        self.assertEqual(bot.quota_today(saved, DAY), 1)
        self.assertEqual(bot.quota_last_7d(saved, DAY), 1)

    def test_reservations_reach_daily_and_weekly_caps_before_browser_start(self):
        for sends, expected in [([NOW] * 19, "cap_reached"),
                                (["2026-11-19T10:00:00+05:30"] * 89, "weekly_cap_reached")]:
            with self.subTest(expected=expected):
                self.ledger = bot.blank_ledger()
                self.ledger["sends"] = sends
                self.ledger["attempts"]["in:unresolved"] = dict(status="uncertain", reserved_ts=NOW)
                starts = []
                summary = bot.run_queue(
                    [CANDIDATE], self.cfg, self.ledger, self.audit, live=True, cap=20,
                    day=DAY, window="00:00-23:59", note=None, limit=0,
                    min_delay=60, max_delay=60, now_iso=NOW,
                    driver_factory=lambda: starts.append("browser"))
                self.assertEqual(summary["stopped"], expected)
                self.assertEqual(starts, [])

    def test_confirmed_send_is_on_disk_before_second_attempt_interrupts(self):
        class Driver:
            count = 0
            def start(self): pass
            def close(self): pass
            def session_is_live(self): return True
            def send_invite(inner, candidate, note):
                inner.count += 1
                if inner.count == 1:
                    return "sent"
                raise KeyboardInterrupt()
        second = dict(CANDIDATE, profile_url="https://www.linkedin.com/in/second/", name="Second")
        with self.assertRaises(KeyboardInterrupt):
            self.run_queue(Driver(), [CANDIDATE, second])
        saved = bot.load_ledger(self.state / bot.LEDGER_NAME)
        self.assertEqual(bot.sends_today(saved, DAY), 1)
        self.assertEqual(bot.quota_today(saved, DAY), 2)
        self.assertEqual(saved["persons"]["in:rangansen"]["status"], "invited")
        self.assertEqual(saved["attempts"]["in:second"]["status"], "uncertain")

    def test_reconciliation_is_auditable_idempotent_and_keeps_uncertain_reserved(self):
        evidence = self.state / "receipt.txt"
        evidence.write_text("Invitation sent to Rangan.\nRangan Sen\nPending\n")
        bot.save_ledger(self.state / bot.LEDGER_NAME, self.ledger)
        original = b'{"decision":"skip","result":"menu_missed"}\n'
        self.audit.write_bytes(original)
        manifest = {"id": "nov20-recovery", "reason": "Interrupted native batch",
                    "records": [{"candidate": CANDIDATE, "outcome": "sent", "ts": NOW,
                                 "evidence": [str(evidence)]},
                                {"candidate": dict(CANDIDATE, profile_url="https://www.linkedin.com/in/koustubh/", name="Koustubh"),
                                 "outcome": "uncertain", "ts": NOW, "evidence": [str(evidence)]}]}
        before = (self.state / bot.LEDGER_NAME).read_bytes()
        preview = bot.reconcile_records(self.state, manifest)
        self.assertFalse(preview["applied"])
        self.assertEqual((self.state / bot.LEDGER_NAME).read_bytes(), before)
        self.assertFalse((self.state / "backups").exists())
        result = bot.reconcile_records(self.state, manifest, apply=True)
        self.assertFalse(result["already_applied"])
        backup = Path(result["backup"])
        self.assertEqual((backup / bot.AUDIT_NAME).read_bytes(), original)
        self.assertTrue(self.audit.read_bytes().startswith(original))
        again = bot.reconcile_records(self.state, manifest, apply=True)
        self.assertTrue(again["already_applied"])
        saved = bot.load_ledger(self.state / bot.LEDGER_NAME)
        self.assertEqual(bot.sends_today(saved, DAY), 1)
        self.assertEqual(bot.quota_today(saved, DAY), 2)
        self.assertEqual(saved["persons"]["in:koustubh"]["status"], "uncertain")
        self.assertEqual(len(saved["reconciliations"]), 1)
        evidence.write_text("changed operator evidence")
        with self.assertRaises(ValueError):
            bot.reconcile_records(self.state, manifest, apply=True)

    def test_potential_action_pacing_survives_non_send_label_and_restart(self):
        class Driver:
            def start(self): pass
            def close(self): pass
            def session_is_live(self): return True
            def send_invite(self, candidate, note):
                self.before_action()
                return "already_connected"
        self.run_queue(Driver())
        self.ledger = bot.load_ledger(self.state / bot.LEDGER_NAME)
        self.assertEqual(bot.quota_today(self.ledger, DAY), 0)
        waits = []
        other = dict(CANDIDATE, profile_url="https://www.linkedin.com/in/second/", name="Second")
        self.run_queue(Driver(), [other], sleeper=waits.append)
        self.assertEqual(waits, [60.0])


if __name__ == "__main__":
    unittest.main()
