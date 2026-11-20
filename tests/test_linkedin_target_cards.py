"""Real sanitized Nov-21 cards plus counterexamples; no account/network access.

Requires optional Playwright + installed Chromium. Every request is fulfilled
locally in a fresh, credential-free context; no canonical profile is opened.
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import linkedin_connect as bot

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin_target_cards"
CANDIDATE = {"name": "Jeffry Issac", "profile_url": "https://www.linkedin.com/in/jeffry-issac/"}
VISIBILITY = '''<style>
[data-readonly-visible="false"]:not(:has([data-readonly-visible="true"])) {display:none}
[data-readonly-visible="false"]:has([data-readonly-visible="true"]) {display:contents}
</style>'''


def card(actions=None, extra="", name="Jeffry Issac"):
    if actions is None:
        actions = '<a href="/preload/custom-invite/" aria-label="Invite Jeffry Issac to connect">Connect</a><button>Message</button><button>More</button>'
    return f'''<main><section id="target"><div id="identity"><div><h2>{name}</h2><span id="degree">· 2nd</span></div><a href="{CANDIDATE['profile_url']}overlay/contact-info/">Contact info</a></div><div id="actions">{actions}</div></section>{extra}</main>'''


class TargetCardDOM(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("optional Playwright is not installed")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(headless=True)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.html = card()
        self.requests = []
        def fulfill(route):
            self.requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=self.html)
        self.context.route("**/*", fulfill)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.actions = []
        self.driver = bot.PlaywrightDriver(Path("unused-credential-free-profile"), capture_dir=self.tmp.name)
        self.driver._ctx = self.context
        self.driver.before_action = lambda: self.actions.append("possible send")

    def page(self, html=None, url=None):
        if html is not None:
            self.html = html
        page = self.context.new_page()
        page.goto(url or CANDIDATE["profile_url"])
        return page

    def quick_readiness(self):
        original = self.driver._await_target_card
        self.driver._await_target_card = lambda p, c: original(p, c, timeout=0.1)

    def diagnostics(self):
        files = list(Path(self.tmp.name).glob("*_preaction.json"))
        self.assertTrue(files, "non-sending failure must be captured before close")
        data = json.loads(files[-1].read_text())
        self.assertEqual(data["final_url"], CANDIDATE["profile_url"])
        self.assertEqual(data["target"]["ready_state"], "complete")
        self.assertTrue(files[-1].with_suffix(".png").is_file())
        return files[-1], data

    def test_all_five_genuine_current_sections_expose_only_their_own_actions(self):
        provenance = json.loads((FIXTURES / "provenance.json").read_text())
        for capture in provenance["captures"]:
            candidate = capture["candidate"]
            slug = candidate["profile_url"].rstrip("/").split("/")[-1]
            with self.subTest(target=slug):
                page = self.page(VISIBILITY + "<main>" + (FIXTURES / (slug + ".html")).read_text() +
                                 '<aside><h2>Suggestion</h2><button>Connect</button><button>Pending</button><button>More</button><span>· 1st</span></aside></main>',
                                 candidate["profile_url"])
                state = self.driver._profile_read(page, candidate)
                self.assertTrue(state["verified"], state)
                self.assertEqual(state["heading_count"], 1)
                self.assertFalse(state["first_degree"], "hidden real 1st placeholder cannot establish connection")
                self.assertFalse(state["pending"])
                self.assertEqual(self.driver._more_buttons(page, candidate).count(), 1)
                self.assertEqual(self.driver._direct_connect_controls(page, candidate).count(),
                                 int(slug in ("jeffry-issac", "bobbymittal")))
                page.close()
        self.assertEqual(self.actions, [])

    def test_nested_identity_sibling_actions_exclude_sidebar_activity_and_sticky_duplicate(self):
        page = self.page(card(extra='''<aside><h2>Someone Else</h2><button>Follow</button><button>Pending</button><button>Connect</button><button>More</button><span>· 1st</span></aside><section><h2>Activity</h2><button>Follow</button></section><section style="position:fixed"><h2>Jeffry Issac</h2><button>Connect</button><button>More</button></section>'''))
        state = self.driver._profile_read(page, CANDIDATE)
        self.assertEqual(state["controls"], ["Invite Jeffry Issac to connect", "Message", "More"])
        self.assertEqual(self.driver._direct_connect_controls(page, CANDIDATE).count(), 1)
        self.assertEqual(self.driver._more_buttons(page, CANDIDATE).count(), 1)
        self.assertIsNone(bot.terminal_outcome(state, CANDIDATE, after_action=False))

    def test_ambiguous_nonsticky_identity_fails_without_any_action_and_captures_both_cards(self):
        self.html = card().replace('</main>', '<section><h2>Jeffry Issac</h2><button>Connect</button></section></main>')

        with self.assertRaises(bot.UnexpectedUI):
            self.driver.send_invite(CANDIDATE, None)
        path, data = self.diagnostics()
        self.assertEqual(data["target"]["heading_count"], 2)
        self.assertEqual(path.with_suffix(".html").read_text().count("<section"), 2)
        self.assertEqual(self.actions, [])
    def test_unrelated_controls_inside_section_cannot_supply_action_or_degree(self):
        html = card().replace('<div id="actions">', '<div><button>Connect</button><button>Pending</button><span>· 1st</span></div><div id="actions">')
        page = self.page(html)
        state = self.driver._profile_read(page, CANDIDATE)
        self.assertFalse(state["first_degree"])
        self.assertFalse(state["pending"])
        self.assertEqual(self.driver._direct_connect_controls(page, CANDIDATE).count(), 1)

    def test_wrong_url_and_wrong_name_never_authorize_a_control(self):
        page = self.page(url="https://www.linkedin.com/in/someone-else/")
        with self.assertRaises(bot.UnexpectedUI):
            self.driver._target_card(page, CANDIDATE)
        page.close()
        self.html = card(name="Someone Else")
        self.quick_readiness()
        self.assertEqual(self.driver.send_invite(CANDIDATE, None), bot.UNKNOWN_CONTROLS)
        self.diagnostics()
        self.assertEqual(self.actions, [])

    def test_accessible_identity_check_is_preserved_and_captured(self):
        self.html = card().replace('<h2>', '<h2 aria-label="Different accessible identity">')
        with self.assertRaises(bot.UnexpectedUI):
            self.driver.send_invite(CANDIDATE, None)
        _, data = self.diagnostics()
        self.assertEqual(data["target"]["heading_accessible_name"], "Different accessible identity")
        self.assertEqual(self.actions, [])

    def test_hydration_waits_for_target_actions_not_body_name_or_sidebar_follow(self):
        page = self.page(card(actions="", extra='<aside><button>Follow</button><button>Connect</button></aside>'))
        page.evaluate('''() => setTimeout(() => {document.querySelector('#actions').innerHTML = '<button>Follow</button><button>Message</button><button>More</button>'}, 100)''')
        state = self.driver._await_target_card(page, CANDIDATE, timeout=2)
        self.assertTrue(state["verified"])
        self.assertEqual(state["controls"], ["Follow", "Message", "More"])
        self.assertEqual(self.actions, [])

    def test_missing_actions_are_unknown_not_sticky_follow_and_capture_complete_section(self):
        self.html = card(actions='<button>Follow</button><button>Message</button>',
                         extra='<aside><button>Follow</button><button>Connect</button></aside>')
        self.quick_readiness()
        self.assertEqual(self.driver.send_invite(CANDIDATE, None), bot.UNKNOWN_CONTROLS)
        path, data = self.diagnostics()
        self.assertEqual(data["target"]["root_count"], 1)
        html = path.with_suffix(".html").read_text()
        self.assertIn("Contact info", html)
        self.assertIn("Message", html)
        self.assertNotIn("<aside", html)
        self.assertEqual(self.actions, [])

    def test_inaccessible_more_is_unknown_even_with_unrelated_follow(self):
        self.html = card(actions='<button>Follow</button><button>Message</button><button>More</button>',
                         extra='<aside><button>Follow</button></aside>')
        class BrokenMore:
            def nth(self, pos): return self
            def click(self, **kwargs): raise RuntimeError("inaccessible control")
        self.driver._more_buttons = lambda page, candidate: BrokenMore()
        self.assertEqual(self.driver.send_invite(CANDIDATE, None), bot.UNKNOWN_CONTROLS)
        self.diagnostics()
        self.assertEqual(self.actions, [])

    def test_verified_target_menu_without_invitation_is_captured_not_a_sidebar_classification(self):
        page = self.page(card(actions='<button>Follow</button><button>Message</button><button id="more">More</button>',
                              extra='<aside><button>Connect</button><button>Follow</button></aside>'))
        page.evaluate('''() => { document.querySelector('#more').addEventListener('click', () => {
          let menu=document.querySelector('[role="menu"]'); if (!menu) {menu=document.createElement('div');menu.setAttribute('role','menu');menu.innerHTML='<li>Follow</li><li>Report</li><li>Block</li>';document.body.append(menu)}
        }); }''')
        with patch.object(page, "wait_for_timeout", return_value=None):
            result = self.driver._more_connect_flow(page, [0], CANDIDATE, None)
        self.assertEqual(result, bot.MENU_MISSED)
        self.driver._capture_target_failure(page, CANDIDATE, result)
        self.diagnostics()
        self.assertEqual(self.actions, [])

    def test_pending_and_first_degree_remain_target_bound(self):
        page = self.page(card(actions='<button>Pending</button><button>Message</button><button>More</button>'))
        state = self.driver._profile_read(page, CANDIDATE)
        self.assertEqual(bot.terminal_outcome(state, CANDIDATE, after_action=False), "already_pending")
        self.assertEqual(bot.terminal_outcome(state, CANDIDATE, after_action=True), "sent")
        page.close()
        page = self.page(card(actions='<button>Message</button><button>More</button>').replace('· 2nd','· 1st'))
        self.assertEqual(bot.terminal_outcome(self.driver._profile_read(page, CANDIDATE), CANDIDATE,
                                            after_action=False), "already_connected")

    def test_sanitized_complete_dom_survives_screenshot_failure(self):
        page = self.page(card().replace('id="actions"', 'id="actions" data-token="secret" onclick="danger()"') + '<script>window.secret="do-not-save"</script>')
        with patch.object(page, "screenshot", side_effect=RuntimeError("screenshot unavailable")):
            self.driver._capture_target_failure(page, CANDIDATE, "control read failed")
        path = next(Path(self.tmp.name).glob("*_preaction.json"))
        data = json.loads(path.read_text())
        self.assertIn("screenshot_error", data)
        html = path.with_suffix(".html").read_text()
        self.assertIn("More", html)
        self.assertIn("Contact info", html)
        self.assertNotIn("secret", html)
        self.assertNotIn("onclick", html)
        self.assertNotIn("<script", html)

    def test_direct_connect_positive_receipt_without_modal_uses_verified_group(self):
        page = self.page()
        page.evaluate('''() => {document.querySelector('#actions a').addEventListener('click', e => {
          e.preventDefault(); e.currentTarget.textContent='Pending'; e.currentTarget.setAttribute('aria-label','Pending');
        });}''')
        self.assertEqual(self.driver._direct_connect_flow(page, CANDIDATE, None, "jeffry"), "sent")
        self.assertEqual(self.actions, ["possible send"])


if __name__ == "__main__":
    unittest.main()
