"""Exercise the actual command with test-only pages; no account or network access."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
from hashlib import sha256
from html import escape
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tools import linkedin_connect as bot

BASE = "https://www.linkedin.com"


def candidate(slug, **overrides):
    return dict({"profile_url": f"{BASE}/in/{slug}/", "name": f"Fixture {slug}",
                 "headline": "Engineer " + "synthetic profile data " * 11}, **overrides)


class OfflinePipeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("optional Playwright is not installed")
        pw = sync_playwright().start()
        cls.addClassCleanup(pw.stop)
        cls.browser = pw.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.state = self.root / "state"
        self.profile = self.root / "profile"
        (self.profile / "Default").mkdir(parents=True)
        (self.profile / "Default" / "Cookies").touch()  # scratch guard sentinel, not browser data
        self.targets = self.root / "targets.json"
        self.ledger_path = self.state / bot.LEDGER_NAME
        self.audit = self.state / bot.AUDIT_NAME
        self.activity = self.root / "activity.json"
        self.evidence = self.root / "activity-fixture.txt"
        self.receipt = self.root / "receipt.txt"
        self.evidence.write_text("Offline complete account activity fixture; not platform evidence.\n")
        self.targets.write_text(json.dumps({
            "min_score": 3, "role_signals": [{"match": "engineer", "weight": 3}],
            "discovery": {"role_seeds": ["stale", "cached", "fresh"],
                          "max_searches": 2, "max_pages": 2},
        }))
        self.assertEqual(self.browser.contexts, [])
        self.now = datetime(2026, 11, 21, 12).astimezone()
        self.visits, self.searches, self.presses = [], [], []
        self.layouts, self.search_cards = {}, {}
        self.starts = 0
        self.bad_routes = []
        self.interrupt_slug = None
        self.closing_slug = None
        self.search_interstitial = None
        owner = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return owner.now.astimezone(tz) if tz is not None else owner.now.replace(tzinfo=None)

        class OfflineDriver(bot.PlaywrightDriver):
            def start(self):
                owner.starts += 1
                self._ctx = owner.browser.new_context(service_workers="block")
                self._ctx.expose_function("fixturePress", owner.press)
                self._ctx.route("**/*", owner.route)

            def _capture(self, page, slug, step, ok, read=None):
                if slug == owner.interrupt_slug and step.endswith("_sent"):
                    owner.receipt.write_text(page.locator("#connect").get_attribute("aria-label"))
                    raise KeyboardInterrupt()

        self.enterContext(patch.object(bot, "PlaywrightDriver", OfflineDriver))
        self.enterContext(patch.object(bot, "datetime", Clock))
        self.enterContext(patch.object(bot.time, "sleep", lambda seconds: self.advance(seconds)))
        from playwright.sync_api import Page
        self.enterContext(patch.object(Page, "wait_for_timeout", return_value=None))

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)

    def press(self, slug, kind):
        self.presses.append((slug, kind, self.now))
        if slug == self.closing_slug:
            self.advance(13 * 3600)

    def route(self, route):
        url = urlparse(route.request.url)
        body = None
        if (self.state / bot.LOCK_NAME).is_file() and url.netloc == "www.linkedin.com":
            if url.path == "/feed/":
                body = "<main>Signed-in test feed</main>"
            elif url.path == "/search/results/people/":
                params = parse_qs(url.query)
                key = params["keywords"][0], int(params["page"][0])
                self.searches.append(key)
                cards = self.search_cards.get(key, [])
                body = "<main>" + ("".join(
                    f'<article><a href="{escape(c["profile_url"])}">{escape(c["name"])}</a>'
                    f'<div>{escape(c["headline"])}</div></article>' for c in cards
                ) or "No results") + "</main>"
                if self.search_interstitial:
                    body = f"<main>{self.search_interstitial}</main>"
            elif url.path.startswith("/in/"):
                slug = url.path.split("/")[2]
                self.visits.append(slug)
                body = self.profile_page(slug)
        if body is None:
            self.bad_routes.append((url.netloc, url.path))
            route.abort()
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    def profile_page(self, slug):
        name = candidate(slug)["name"]
        kind = self.layouts.get(slug, "direct")
        actions = {
            "direct": '<button id="connect">Connect</button>',
            "pending": f'<button aria-label="Pending, click to withdraw invitation sent to {name}">Pending</button>',
            "miss": '<button>Follow</button>',
            "modal": '<button>Follow</button>',
        }[kind]
        menu = (f'<div role="menu" id="menu" hidden><a role="menuitem" '
                f'aria-label="Invite {name} to connect" id="entry">Connect</a></div>') if kind == "modal" else ""
        return f'''<main><section><div><h2>{name}</h2><span>· 2nd</span>
          <a href="/in/{slug}/overlay/contact-info/">Contact info</a></div>
          <div id="actions">{actions}<button>Message</button><button id="more">More</button></div>
          </section></main>{menu}<script>
          const name = {json.dumps(name)}, slug = {json.dumps(slug)};
          const done = stage => {{
            fixturePress(slug, stage);
            let status = document.querySelector('#connect');
            if (!status) {{
              status = document.createElement('button'); status.id = 'connect';
              document.querySelector('#actions').prepend(status);
            }}
            status.textContent = 'Pending';
            status.setAttribute('aria-label', 'Pending, click to withdraw invitation sent to ' + name);
          }};
          document.querySelector('#connect')?.addEventListener('click', () => done('direct'));
          const menu = document.querySelector('#menu');
          if (menu) {{
            document.querySelector('#more').onclick = () => menu.hidden = !menu.hidden;
            document.querySelector('#entry').onclick = () => {{
              fixturePress(slug, 'connect'); menu.hidden = true;
              const dialog = document.createElement('div'); dialog.setAttribute('role', 'dialog');
              dialog.innerHTML = `Personalize your invitation to ${{name}} <button>Send without a note</button>`;
              dialog.querySelector('button').onclick = () => {{ dialog.remove(); done('send'); }};
              document.body.append(dialog);
            }};
          }}
          </script>'''

    def add_queue(self, *slugs):
        ledger = bot.load_ledger(self.ledger_path)
        for slug in slugs:
            ledger["persons"][f"in:{slug}"] = candidate(slug, status="queued")
        bot.save_ledger(self.ledger_path, ledger)

    def write_activity(self, day=0, week=0, **overrides):
        self.activity.write_text(json.dumps(dict({
            "complete": True, "observed_at": self.now.isoformat(),
            "ledger_sha256": sha256(self.ledger_path.read_bytes()).hexdigest(),
            "used_today_upper": day, "used_7d_upper": week, "evidence": [self.evidence.name],
        }, **overrides)))

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with patch("sys.argv", ["linkedin_connect.py", *args, "--state-dir", str(self.state)]), \
                redirect_stdout(out), redirect_stderr(err):
            code = bot.main()
        self.assertFalse((self.state / bot.LOCK_NAME).exists())
        self.assertEqual(self.browser.contexts, [])
        self.assertEqual(self.bad_routes, [])
        return code, out.getvalue(), err.getvalue()

    def dispatch(self, goal, *, activity=True):
        return self.cli("run", "--live", "--goal", str(goal), "--profile-dir", str(self.profile),
                        "--targets", str(self.targets), "--min-delay", "60", "--max-delay", "60",
                        *(["--account-activity", str(self.activity)] if activity else []))

    def test_ten_confirmed_with_one_owner_and_bounded_replenishment(self):
        self.add_queue("pending", "miss", "initial")
        self.layouts.update(pending="pending", miss="miss", cached1="modal")
        ledger = bot.load_ledger(self.ledger_path)
        ledger["persons"]["in:previous"] = candidate("previous", status="invited")
        ledger["sends"] = [self.now.isoformat()]
        bot.save_ledger(self.ledger_path, ledger)
        previous = candidate("previous", profile_url=f"{BASE}/in/PREVIOUS/?source=fixture")
        cached = [candidate(f"cached{i}") for i in range(6)]
        bot.save_cache(self.state / bot.CACHE_NAME, {"pages": {
            bot.cache_key("stale", "", 1): {"results": [previous]},
            bot.cache_key("stale", "", 2): {"results": []},
            bot.cache_key("cached", "", 1): {"results": [*cached, previous]},
            bot.cache_key("cached", "", 2): {"results": [cached[0]]},
        }})
        fresh = [candidate(f"fresh{i}") for i in range(6)]
        self.search_cards[("fresh", 1)] = [*fresh, candidate("irrelevant", headline="Unrelated")]
        self.write_activity(day=10, week=80)
        code, out, err = self.dispatch(10)
        self.assertEqual(code, 0, (out, err))
        self.assertIn("10/10 confirmed", out)
        result = bot.load_ledger(self.ledger_path)
        stats = result["last_run"]["summary"]
        self.assertEqual((stats["sent"], stats["skipped"], stats["already_pending"],
                          stats["uncertain"], stats["unattempted"]), (10, 1, 1, 0, 3))
        self.assertEqual((self.starts, self.searches), (1, [("fresh", 1), ("fresh", 2)]))
        self.assertNotIn("previous", self.visits)
        self.assertNotIn("irrelevant", self.visits)
        self.assertEqual(len(self.visits), len(set(self.visits)))
        deliveries = [ts for _, action, ts in self.presses if action in ("direct", "send")]
        self.assertEqual(len(deliveries), 10)
        self.assertTrue(all((b - a).total_seconds() >= 60 for a, b in zip(deliveries, deliveries[1:])))
        self.assertEqual(result["sends"][:1], ledger["sends"])
        self.assertEqual(bot.quota_today(result, self.now.date().isoformat()), 11)
        self.assertEqual(bot.unresolved_attempts(result), {})

    def test_interruption_requires_backed_up_reconciliation_and_never_resends(self):
        self.add_queue("one", "two", "three")
        self.write_activity()
        self.interrupt_slug = "in-two"
        code, out, err = self.dispatch(3)
        self.assertEqual(code, 130, err)
        self.assertEqual(self.visits, ["one", "two"])
        self.assertEqual(self.receipt.read_text(), "Pending, click to withdraw invitation sent to Fixture two")
        result = bot.load_ledger(self.ledger_path)
        attempt = result["attempts"]["in:two"]
        self.assertEqual((attempt["status"], attempt["actions"]), ("uncertain", 1))
        self.assertEqual(tuple(json.loads(out)["summary"][f] for f in ("sent", "uncertain", "unattempted")), (1, 1, 1))
        code, _, err = self.dispatch(1, activity=False)
        self.assertEqual(code, bot.EXIT_STOPPED, err)
        self.assertEqual(self.starts, 1)
        audit = self.audit.read_bytes()
        manifest = self.root / "reconcile.json"
        manifest.write_text(json.dumps({"id": "offline-interruption", "reason": "Fixture Pending receipt",
            "records": [{"candidate": candidate("two"), "outcome": "sent", "ts": attempt["reserved_ts"],
                         "evidence": [str(self.receipt)]}]}))
        code, out, err = self.cli("reconcile", "--manifest", str(manifest), "--apply")
        self.assertEqual(code, 0, err)
        self.assertEqual((Path(json.loads(out)["backup"]) / bot.AUDIT_NAME).read_bytes(), audit)
        self.assertTrue(self.audit.read_bytes().startswith(audit))
        self.assertEqual(bot.load_ledger(self.ledger_path)["sends"], [*result["sends"], attempt["reserved_ts"]])
        self.write_activity(day=2, week=2)
        code, out, err = self.dispatch(1)
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.visits, ["one", "two", "three"])
        self.assertEqual([slug for slug, _, _ in self.presses], ["one", "two", "three"])

    def test_confirmation_after_window_closes_is_persisted_without_another_action(self):
        self.add_queue("last", "deferred")
        self.write_activity()
        action_time = self.now.isoformat()
        self.closing_slug = "last"
        code, out, err = self.dispatch(2)
        self.assertEqual(code, 1, (out, err))
        self.assertIn("1/2 confirmed", out)
        self.assertEqual(self.visits, ["last"])
        self.assertEqual(bot.load_ledger(self.ledger_path)["sends"], [action_time])
        self.assertEqual(bot.unresolved_attempts(bot.load_ledger(self.ledger_path)), {})

    def test_discovery_restriction_stops_without_another_search_or_invite(self):
        self.add_queue()
        self.write_activity()
        self.search_interstitial = "Weekly limit — try again later."
        code, out, err = self.dispatch(5)
        self.assertEqual(code, bot.EXIT_STOPPED, (out, err))
        self.assertEqual(json.loads(out)["summary"]["stopped"], "rate_limited")
        self.assertEqual((len(self.searches), self.presses, self.visits), (1, [], []))

    def test_refuses_incomplete_or_insufficient_external_capacity_before_browser(self):
        self.add_queue("unused")
        for overrides in ({"complete": False}, {"used_today_upper": 16, "used_7d_upper": 16},
                          {"used_7d_upper": 86}, {"ledger_sha256": "stale"}, {"evidence": []}):
            with self.subTest(overrides=overrides):
                self.write_activity(**overrides)
                code, _, err = self.dispatch(5)
                self.assertEqual(code, bot.EXIT_STOPPED, err)
        self.assertEqual(self.starts, 0)
        self.assertEqual(bot.load_ledger(self.ledger_path)["attempts"], {})
