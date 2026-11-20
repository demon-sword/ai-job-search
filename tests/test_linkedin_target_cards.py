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

    def test_saved_long_pending_receipt_needs_no_first_name_toast_or_press(self):
        html = VISIBILITY + "<main>" + (FIXTURES / "jeffry-issac-pending.html").read_text() + "</main>"
        for toast in ("", '<div role="status">Invitation sent to Jeffry.</div>'):
            with self.subTest(toast=toast):
                page = self.page(html + toast)
                state = self.driver._profile_read(page, CANDIDATE)
                self.assertTrue(state["verified"])
                self.assertEqual(bot.terminal_outcome(state, CANDIDATE, after_action=False), "already_pending")
                self.assertEqual(bot.terminal_outcome(state, CANDIDATE, after_action=True), "sent")
                page.close()
        self.assertEqual(self.actions, [])

    def test_long_pending_is_exact_person_and_action_group_only(self):
        label = "Pending, click to withdraw invitation sent to Jeffry Issac"
        for wrong in ("Jeffry", "Jeffry Issac Jr.", "Someone Else"):
            with self.subTest(wrong=wrong):
                page = self.page(card(
                    actions=f'<a aria-label="{label.replace("Jeffry Issac", wrong)}">Pending</a><button>Message</button><button>More</button>',
                    extra=f'<aside><a aria-label="{label}">Pending</a></aside><div role="status">Invitation sent to Jeffry.</div>'))
                state = self.driver._profile_read(page, CANDIDATE)
                self.assertFalse(state["pending"])
                self.assertIsNone(bot.terminal_outcome(state, CANDIDATE, after_action=True))
                page.close()
        page = self.page(card(extra=f'<aside><a aria-label="{label}">Pending</a></aside>'))
        self.assertFalse(self.driver._profile_read(page, CANDIDATE)["pending"])
        self.assertEqual(self.actions, [])

    def test_delayed_long_pending_acknowledges_once_but_first_name_toast_alone_does_not(self):
        for acknowledgement in (True, False):
            with self.subTest(acknowledgement=acknowledgement):
                self.actions.clear()
                page = self.page()
                page.evaluate('''ack => {
                  window.connectPresses=0;
                  document.querySelector('#actions a').addEventListener('click', e => {
                    e.preventDefault(); window.connectPresses++;
                    const toast=document.createElement('div'); toast.setAttribute('role','status');
                    toast.textContent='Invitation sent to Jeffry.'; document.body.append(toast);
                    if(ack) setTimeout(() => {
                      e.target.textContent='Pending';
                      e.target.setAttribute('aria-label','Pending, click to withdraw invitation sent to Jeffry Issac');
                    }, 80);
                  });
                }''', acknowledgement)
                if acknowledgement:
                    self.assertEqual(self.driver._direct_connect_flow(page, CANDIDATE, None, "jeffry"), "sent")
                else:
                    with self.assertRaises(bot.UncertainSend):
                        self.driver._direct_connect_flow(page, CANDIDATE, None, "jeffry")
                self.assertEqual(page.evaluate("window.connectPresses"), 1)
                self.assertEqual(self.actions, ["possible send"])
                page.close()

    def menu_page(self, entries, *, second_menu="", candidate=None):
        # Actual captured Vinaayak section, SYNTHETIC menu: acceptance retained
        # names/screenshot, not exact menu markup. Never claim byte-faithful replay.
        candidate = candidate or {"name": "Vinaayak O.", "profile_url": "https://www.linkedin.com/in/vinayak-ojha/"}
        html = VISIBILITY + "<main>" + (FIXTURES / "vinayak-ojha.html").read_text() + "</main>"
        page = self.page(html + f'<div id="menu" role="menu" hidden>{entries}</div>' + second_menu,
                         candidate["profile_url"])
        more = self.driver._more_buttons(page, candidate)
        more.evaluate('''button => {
          window.entryPresses=[];
          button.addEventListener('click', () => {
            const menu=document.querySelector('#menu'); menu.hidden=!menu.hidden;
          });
          document.addEventListener('keydown', e => {
            if(e.key==='Escape') document.querySelector('#menu').hidden=true;
          });
          document.querySelectorAll('#menu li').forEach(entry => {
            entry.addEventListener('click', () => {
              window.entryPresses.push(entry.getAttribute('aria-label') || entry.textContent);
              const pending=document.createElement('a'); pending.textContent='Pending';
              pending.setAttribute('aria-label','Pending, click to withdraw invitation sent to Vinaayak O.');
              button.parentElement.append(pending);
              document.querySelector('#menu').hidden=true;
            });
            entry.addEventListener('keydown', e => {if(e.key==='Enter') entry.click()});
          });
        }''')
        return page, candidate

    def test_exact_accessible_invitation_entry_and_plain_li_reach_only_target_once(self):
        for row in ('<li aria-label="Invite Vinaayak O. to connect" tabindex="0"><span>Connect</span></li>',
                    '<li tabindex="0">Connect</li>'):
            with self.subTest(row=row):
                self.actions.clear()
                page, candidate = self.menu_page(row)
                with patch.object(page, "wait_for_timeout", return_value=None):
                    self.assertEqual(self.driver._more_connect_flow(page, [0], candidate, None), "sent")
                self.assertEqual(page.evaluate("window.entryPresses"),
                                 ["Invite Vinaayak O. to connect" if "aria-label" in row else "Connect"])
                self.assertEqual(self.actions, ["possible send"])
                page.close()

    def test_ambiguous_wrong_person_destructive_and_note_only_menus_never_press(self):
        named = '<li aria-label="Invite Vinaayak O. to connect"><span>Connect</span></li>'
        cases = [
            (named + named, ""),
            (named, '<div role="menu"><li>Connect</li></div>'),
            ('<li aria-label="Invite Someone Else to connect"><span>Connect</span></li>', ""),
            ('<li>Withdraw invitation</li><li>Follow</li><li>Report Vinaayak</li><li>Block Vinaayak</li>', ""),
            ('<li aria-label="Invite Vinaayak O. to connect">Withdraw invitation</li>', ""),
            ('<li aria-label="Invite Vinaayak O. to connect">Send a note</li>', ""),
            ('<li aria-disabled="true">Connect</li>', ""),
            ('<li>Send a note to Vinaayak O.</li>', ""),
            ("", '<aside><div role="menu"><li>Connect</li></div></aside>'),
        ]
        for row, extra in cases:
            with self.subTest(row=row, extra=extra):
                page, candidate = self.menu_page(row, second_menu=extra)
                with patch.object(page, "wait_for_timeout", return_value=None):
                    result = self.driver._more_connect_flow(page, [0], candidate, None)
                self.assertIn(result, (bot.MENU_MISSED, bot.DIALOG_MISSED))
                self.assertEqual(page.evaluate("window.entryPresses"), [])
                self.assertEqual(self.actions, [])
                page.close()

    def test_hidden_named_copy_does_not_mask_visible_connect(self):
        page, candidate = self.menu_page(
            '<li tabindex="0">Connect</li>',
            second_menu='<div role="menu" hidden><li aria-label="Invite Vinaayak O. to connect">Connect</li></div>')
        with patch.object(page, "wait_for_timeout", return_value=None):
            self.assertEqual(self.driver._more_connect_flow(page, [0], candidate, None), "sent")
        self.assertEqual(page.evaluate("window.entryPresses"), ["Connect"])
        self.assertEqual(self.actions, ["possible send"])

    def test_keyboard_fallback_closes_open_menu_before_reexpanding(self):
        page, candidate = self.menu_page(
            '<li aria-label="Invite Vinaayak O. to connect" tabindex="0"><span>Connect</span></li>')
        with patch.object(page, "wait_for_timeout", return_value=None), \
                patch.object(self.driver, "_menu_entry_box", return_value=None):
            self.assertEqual(self.driver._more_connect_flow(page, [0], candidate, None), "sent")
        self.assertEqual(page.evaluate("window.entryPresses"), ["Invite Vinaayak O. to connect"])
        self.assertEqual(self.actions, ["possible send"])


if __name__ == "__main__":
    unittest.main()
