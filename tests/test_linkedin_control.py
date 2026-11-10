"""Control levers for tools/linkedin_connect.py: on-demand dispatch, stable login, one status command.

Lever 1 (dispatch): the default --state-dir resolves outside the repo so
disposable worktrees can never reset caps or split the ledger; one batch
holds the state-dir lock at a time.
Lever 2 (login): a guard refuses automated browser loads while the login
window is open or the profile was touched outside the tool.
Lever 3 (status): plain-words counts, last error, and session liveness with
no browser start and no third-party imports.
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
TOOL = REPO / "tools" / "linkedin_connect.py"
DISPATCH_DOC = REPO / "tools" / "LINKEDIN_DISPATCH.md"
LOGIN_DOC = REPO / "tools" / "LINKEDIN_LOGIN.md"
DAY = "2026-11-01"
NOW = f"{DAY}T10:00:00+05:30"
ALWAYS = "00:00-23:59"  # window that always contains local time


def fresh_home(testcase):
    """Point HOME at a scratch dir so default paths resolve there."""
    tmp = TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    old = os.environ.get("HOME")
    os.environ["HOME"] = tmp.name
    testcase.addCleanup(lambda: os.environ.__setitem__("HOME", old) if old is not None
                        else os.environ.pop("HOME", None))
    return Path(tmp.name)


class StateDirDefault(unittest.TestCase):
    def test_default_state_dir_lives_outside_the_repo(self):
        from tools import linkedin_connect as mod

        default = mod.default_state_dir()
        self.assertTrue(default.is_absolute(), default)
        self.assertNotIn(REPO, default.parents, f"{default} must not sit under the repo {REPO}")
        self.assertEqual(default, mod.DEFAULT_STATE_DIR)
        self.assertEqual(default.relative_to(Path.home()).parts[:1],
                         (".linkedin-connect-bot",))

    def test_worktree_run_resolves_state_outside_the_repo(self):
        """A batch launched from any worktree must report state outside it."""
        home = fresh_home(self)
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--json"],
            capture_output=True, text=True, cwd=str(REPO),
            env={**os.environ, "HOME": str(home)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ledger = json.loads(proc.stdout)["ledger"]
        self.assertNotIn(str(REPO), ledger, "state must not resolve under the worktree")
        self.assertTrue(str(ledger).startswith(str(home)), ledger)

    def test_explicit_state_dir_still_wins(self):
        from tools import linkedin_connect as mod

        self.assertEqual(mod.resolve_state_dir(Path("/tmp/x")), Path("/tmp/x"))

    def test_legacy_in_repo_ledger_blocks_a_quiet_default(self):
        """Fail safe, never split the ledger: a legacy in-repo ledger with
        no migrated copy refuses the default with the one-time move."""
        from tools import linkedin_connect as mod

        home = fresh_home(self)
        legacy = home / "legacy"
        legacy.mkdir()
        (legacy / "ledger.json").write_text(json.dumps({"persons": {}, "sends": []}),
                                            encoding="utf-8")
        real_legacy, real_default = mod.LEGACY_STATE_DIR, mod.DEFAULT_STATE_DIR
        mod.LEGACY_STATE_DIR = legacy
        mod.DEFAULT_STATE_DIR = home / "state"
        self.addCleanup(setattr, mod, "LEGACY_STATE_DIR", real_legacy)
        self.addCleanup(setattr, mod, "DEFAULT_STATE_DIR", real_default)
        with self.assertRaises(SystemExit) as ctx:
            mod.resolve_state_dir(None)
        self.assertIn("mv", str(ctx.exception))


class BatchLock(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()

    def test_second_holder_is_refused(self):
        from tools import linkedin_connect as mod

        first = mod.acquire_batch_lock(self.state, "run")
        try:
            with self.assertRaises(mod.BatchLocked):
                mod.acquire_batch_lock(self.state, "discover")
        finally:
            first.release()
        second = mod.acquire_batch_lock(self.state, "discover")
        second.release()

    def test_stale_lock_from_a_dead_pid_is_taken_over(self):
        from tools import linkedin_connect as mod

        proc = subprocess.Popen(["true"])
        proc.wait()
        stale = {"pid": proc.pid, "cmd": "run", "started": NOW}
        (self.state / mod.LOCK_NAME).write_text(json.dumps(stale), encoding="utf-8")
        lock = mod.acquire_batch_lock(self.state, "run")
        lock.release()

    def test_cli_live_run_refuses_when_the_lock_is_held(self):
        from tools import linkedin_connect as mod

        holder = {"pid": os.getpid(), "cmd": "discover", "started": NOW}
        (self.state / mod.LOCK_NAME).write_text(json.dumps(holder), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOL), "run", "--from-ledger", "--live",
             "--state-dir", str(self.state), "--window", ALWAYS],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lock", proc.stderr.lower())


class LoginGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()
        self.profile = Path(self._tmp.name) / "profile"
        (self.profile / "Network").mkdir(parents=True)
        self.cookies = self.profile / "Network" / "Cookies"
        self.cookies.write_bytes(b"sqlite")

    def test_open_login_window_refuses_a_browser_load(self):
        from tools import linkedin_connect as mod

        (self.state / mod.LOGIN_MARKER_NAME).write_text(
            json.dumps({"pid": os.getpid(), "started": NOW}), encoding="utf-8")
        reason = mod.check_profile_load(self.state, self.profile)
        self.assertIsNotNone(reason)
        self.assertIn("login", reason.lower())

    def test_externally_touched_profile_refuses_a_browser_load(self):
        from tools import linkedin_connect as mod

        self.assertIsNone(mod.check_profile_load(self.state, self.profile))
        mod.note_guarded_load(self.state, self.profile, "run")
        os.utime(self.cookies, (os.path.getatime(self.cookies),
                                os.path.getmtime(self.cookies) + 120))
        reason = mod.check_profile_load(self.state, self.profile)
        self.assertIsNotNone(reason)
        self.assertIn("outside", reason.lower())

    def test_untouched_profile_loads_and_is_noted(self):
        from tools import linkedin_connect as mod

        self.assertIsNone(mod.check_profile_load(self.state, self.profile))
        mod.note_guarded_load(self.state, self.profile, "run")
        self.assertIsNone(mod.check_profile_load(self.state, self.profile))

    def test_cli_live_run_refuses_during_the_login_window(self):
        from tools import linkedin_connect as mod

        (self.state / mod.LOGIN_MARKER_NAME).write_text(
            json.dumps({"pid": os.getpid(), "started": NOW}), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOL), "run", "--from-ledger", "--live",
             "--state-dir", str(self.state),
             "--profile-dir", str(self.profile), "--window", ALWAYS],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("login", proc.stderr.lower())

    def test_login_refuses_while_a_batch_holds_the_lock(self):
        from tools import linkedin_connect as mod

        holder = {"pid": os.getpid(), "cmd": "run", "started": NOW}
        (self.state / mod.LOCK_NAME).write_text(json.dumps(holder), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(TOOL), "login", "--state-dir", str(self.state),
             "--profile-dir", str(self.profile)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("lock", proc.stderr.lower())


class StatusPlain(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.state.mkdir()
        self.profile = Path(self._tmp.name) / "profile"
        (self.profile / "Network").mkdir(parents=True)

    def _seed(self, sends, audit_rows=()):
        from tools.linkedin_connect import save_ledger, append_audit

        save_ledger(self.state / "ledger.json",
                    {"persons": {"in:a": {"status": "invited"}}, "sends": sends,
                     "queued_log": []})
        for row in audit_rows:
            append_audit(self.state / "audit.jsonl", row)

    def test_status_defaults_to_plain_words(self):
        from datetime import datetime

        today = datetime.now().astimezone().date().isoformat()
        self._seed([f"{today}T08:00:00+05:30"] * 3)
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--state-dir", str(self.state),
             "--profile-dir", str(self.profile)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("{", proc.stdout.splitlines()[0])
        for needle in ["Invites sent today: 3", "of 20", "week", "of 90",
                       "Last error:", "Session:"]:
            self.assertIn(needle, proc.stdout)

    def test_status_shows_week_count_and_last_error_on_one_line(self):
        from datetime import datetime, timedelta

        today = datetime.now().astimezone().date()
        recent = (today - timedelta(days=3)).isoformat()
        sends = ([f"{today.isoformat()}T08:00:00+05:30"] * 2
                 + [f"{recent}T08:00:00+05:30"] * 4)
        self._seed(sends, [{"ts": NOW, "decision": "stop",
                            "reason": "login_challenge: auth wall at /login",
                            "live": True}])
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--state-dir", str(self.state),
             "--profile-dir", str(self.profile)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Invites this week: 6 of 90", proc.stdout)
        last = next(line for line in proc.stdout.splitlines()
                    if line.startswith("Last error:"))
        self.assertIn("login_challenge", last)

    def test_status_json_flag_keeps_the_machine_shape(self):
        from datetime import datetime

        today = datetime.now().astimezone().date().isoformat()
        self._seed([f"{today}T08:00:00+05:30"])
        proc = subprocess.run(
            [sys.executable, str(TOOL), "status", "--json",
             "--state-dir", str(self.state), "--profile-dir", str(self.profile)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["sent_today"], 1)
        self.assertEqual(out["sent_last_7d"], 1)


class DispatchDoc(unittest.TestCase):
    def test_dispatch_doc_covers_the_batch_template(self):
        text = DISPATCH_DOC.read_text(encoding="utf-8")
        for needle in ["quota", "queue source", "live flag", "window deadline", "lock"]:
            self.assertIn(needle, text.lower())

    def test_login_doc_covers_the_ceremony_and_keep_alive(self):
        text = LOGIN_DOC.read_text(encoding="utf-8")
        for needle in ["ceremony", "one window", "keep", "login"]:
            self.assertIn(needle, text.lower())


if __name__ == "__main__":
    unittest.main()
