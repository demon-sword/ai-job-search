#!/usr/bin/env python3
"""LinkedIn connection-request bot: invite hiring people who can help the search.

> Personal use only. Sending automated connection requests violates LinkedIn's
> Terms of Service and risks account restrictions or a ban. The operator was
> warned and explicitly accepted that risk. Keep volume low, never use this
> commercially, and never let CI or any scheduler run it unattended.

What it does: takes a queue of candidate profiles (LinkedIn people URLs the
operator collected by hand - the bot never scrapes people-search pages),
scores each one against an explicit, configurable targeting file
(`tools/linkedin_connect_targets.json`), and sends connection invites to the
matches - at most HARD_DAILY_CAP per day, paced with jitter, inside a daily
window, with every decision appended to a local audit log.

Safety rails (all enforced in code, all covered by tests/test_linkedin_connect.py):
1. Hard daily cap of 20 invites, counted from a durable local ledger
   (`linkedin_connect/ledger.json`) that survives restarts. `--max-per-day`
   can only lower the cap, never raise it.
2. Dry-run default. Without `--live` the bot prints exactly what it WOULD
   send and sends nothing: no browser starts, the ledger is untouched.
3. Instant full stop - no retry, loud report, exit code 2 - on any login
   challenge, captcha, verification prompt, rate-limit response, or
   unexpected LinkedIn UI state.
4. Human-like pacing: a jittered sleep between invites, and sending is
   refused outright outside the configured daily window.
5. Append-only audit log (`linkedin_connect/audit.jsonl`): who, why they
   matched, timestamp, result. Never rewritten, only appended.
6. No credentials or session tokens anywhere in the repo, logs, or
   artifacts. Login is a persistent local browser profile the operator
   creates once by hand (`login` subcommand); secrets never enter this tool.
7. Dedupe: a person already invited, connected, declined, or withdrawn is
   never re-invited or re-queued.

There is no official LinkedIn invite API, so live sending drives a real
browser via Playwright (optional dependency, needed only for `--live`).
The invite mechanics this automates: open the profile, press Connect
(or More -> Connect), optionally attach a <=300-character note, Send.
A "Pending" badge means already invited, a "Message" button means already
connected - both are recorded, neither counts toward the cap.

Usage:
  python3 tools/linkedin_connect.py plan --candidates queue.json
  python3 tools/linkedin_connect.py discover                            # dry-run: what would be searched/queued
  python3 tools/linkedin_connect.py discover --live                     # search people-search, queue matches
  python3 tools/linkedin_connect.py run --candidates queue.json        # dry-run
  python3 tools/linkedin_connect.py run --from-ledger --live            # invite discover-queued profiles
  python3 tools/linkedin_connect.py run --candidates queue.json --live  # sends
  python3 tools/linkedin_connect.py status
  python3 tools/linkedin_connect.py login                               # one-time manual login

Candidates file: JSON array of {"profile_url": ..., "name": ...,
"headline": ..., "location": ..., "company": ...}. Only profile_url is
required; the rest feed the targeting score and the audit log.

Stdlib only unless --live (which needs `pip install playwright` plus
`python -m playwright install chromium`). Exit 0 on success, 1 on a usage
or state error, 2 on a safety stop.
"""

import argparse
import json
import os
import random
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = ROOT / "tools" / "linkedin_connect_targets.json"
DEFAULT_STATE_DIR = ROOT / "linkedin_connect"
LEDGER_NAME = "ledger.json"
AUDIT_NAME = "audit.jsonl"

HARD_DAILY_CAP = 20
DEFAULT_MIN_DELAY = 60.0
DEFAULT_MAX_DELAY = 180.0
DEFAULT_WINDOW = "09:00-21:00"
NOTE_LIMIT = 300

EXIT_STOPPED = 2

TERMINAL_STATUSES = {"invited", "connected", "declined", "withdrawn"}

# Anything that looks like a secret is scrubbed before it can reach the
# audit log, stdout summaries, or error messages. Login state lives in the
# browser profile directory, so these patterns should never fire - they are
# the backstop, not the mechanism.
SECRET_PATTERNS = re.compile(
    r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|session[_-]?token|auth[_-]?token|secret|cookie|set-cookie|li_at|JSESSIONID)\b"
    r"\s*[:=]\s*\S+"
)


def scrub(text: str) -> str:
    """Replace credential-looking fragments with a redaction marker."""
    return SECRET_PATTERNS.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + "=***", str(text))


def utcnow_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str(now: datetime | None = None) -> str:
    return (now or datetime.now().astimezone()).date().isoformat()


class InviteStop(Exception):
    """Fatal safety stop: abort the run immediately, no retry."""

    signal: str = "stop"
    loud: str = "LinkedIn safety stop"


class LoginChallenge(InviteStop):
    signal = "login_challenge"
    loud = "LinkedIn asked for a login (auth wall or signed-out session)"


class CaptchaChallenge(InviteStop):
    signal = "captcha"
    loud = "LinkedIn showed a captcha challenge"


class VerificationPrompt(InviteStop):
    signal = "verification"
    loud = "LinkedIn showed an identity/security verification prompt"


class RateLimited(InviteStop):
    signal = "rate_limited"
    loud = "LinkedIn rate-limited the invites (limit banner or HTTP 429/999)"


class UnexpectedUI(InviteStop):
    signal = "unexpected_ui"
    loud = "LinkedIn showed an unexpected page state (no Connect control, no known badge)"


def normalize_button_name(raw: str) -> str:
    """Normalize one control name for comparison. Visible names carry icon
    glyphs ("+ Follow") that exact matching would otherwise miss; the strip
    is leading-symbols-only, so "Invite X to connect" never collapses onto
    "Connect" and a loose match can never click a wrong control again."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    return re.sub(r"^[^A-Za-z0-9]+", "", text)


def classify_invite_entry(names) -> str:
    """Classify a profile's top-card action row from normalized control
    names. Callers pass the card-scoped set (see PlaywrightDriver: anchored
    on the card's Contact-info link, never page-wide, so a suggested
    profile's Connect can never be mistaken for this profile's).

    'connect' means a Connect control exists and the dialog flow may
    proceed. 'follow_only' means a Follow control exists but no Connect -
    a creator-mode profile that can never receive an invite; it is skipped
    with a reason, never force-clicked. Anything else is 'unknown' and stays
    a fail-closed UnexpectedUI. Message may co-occur with Follow on open
    2nd-degree profiles, so it never implies anything here.
    """
    normalized = {normalize_button_name(name) for name in names or []}
    normalized.discard("")
    if "Connect" in normalized:
        return "connect"
    if "Follow" in normalized:
        return "follow_only"
    return "unknown"


def classify_entry_from_texts(lines) -> str | None:
    """Second opinion from the card's visible text lines: an exact "Connect"
    line means connect, an exact "Follow" line with no Connect line means
    follow_only, anything else is None (no opinion). Used against the
    accessible-name signal by resolve_entry; never drives a click alone."""
    texts = [normalize_button_name(line) for line in lines or []]
    if "Connect" in texts:
        return "connect"
    if "Follow" in texts:
        return "follow_only"
    return None


def resolve_entry(acc_entry: str, text_entry: str | None) -> str:
    """Combine the accessible-name signal with the visible-text signal.

    Clicks require an acc-verified control, so text alone never yields
    connect. Skips are safe, so a lone follow_only signal suffices. Genuine
    conflicts and double silence stay fail-closed unknown.
    """
    if acc_entry == "connect" and text_entry in ("connect", None):
        return "connect"
    if acc_entry == "follow_only" and text_entry in ("follow_only", None):
        return "follow_only"
    if acc_entry == "unknown" and text_entry == "follow_only":
        return "follow_only"
    return "unknown"


FOLLOW_ONLY = "follow_only"
NOTE_REQUIRED = "note_required"
UNKNOWN_CONTROLS = "unknown_controls"
# run skips these without sending; discovery never re-queues them.
# unknown_controls is deliberately NOT sticky: an unreadable profile is
# retried next run, while the per-run storm guard watches for UI drift.
RUN_SKIP_STATUSES = TERMINAL_STATUSES | {FOLLOW_ONLY, NOTE_REQUIRED}
DISCOVERY_STATUSES = TERMINAL_STATUSES | {"queued", FOLLOW_ONLY, NOTE_REQUIRED}
MAX_CONSECUTIVE_UNKNOWN = 3

_INVITE_ENTRY_RE = re.compile(r"^Invite (.+) to connect$", re.IGNORECASE)


def pick_invite_entry(names, candidate_name: str) -> str | None:
    """Choose the exact More-menu string to click, or None. Only a full
    "Invite <candidate name> to connect" match (case-insensitive) is
    clickable: a bare "Connect" anywhere could belong to a suggested
    profile, and clicking that invites a stranger. Returns the observed
    string verbatim for an exact-text click - never a reconstruction."""
    want = (candidate_name or "").strip().lower()
    for raw in names or []:
        match = _INVITE_ENTRY_RE.match(str(raw or "").strip())
        if match and match.group(1).strip().lower() == want and want:
            return str(raw).strip()
    return None


_CHALLENGE_URL = re.compile(r"/checkpoint/challenge", re.I)
_AUTHWALL_URL = re.compile(r"authwall|/login|/uas/login", re.I)

# People-search card extraction deliberately avoids class selectors:
# LinkedIn serves obfuscated, rotating classes (b6bayf-style), so the only
# stable hooks are the /in/ profile hrefs and the card's ordered text lines.
# The browser returns raw (url, link text, lines); parse_search_card below
# turns lines into fields in pure Python, which is what the tests pin.
SEARCH_CARDS_JS = """() => {
    const seen = new Set();
    const cards = [];
    for (const link of document.querySelectorAll('main a[href*="/in/"]')) {
        const href = (link.href || '').split('?')[0];
        if (!href || seen.has(href)) continue;
        seen.add(href);
        let scope = link;
        for (let depth = 0; depth < 8; depth++) {
            const parent = scope.parentElement;
            if (!parent || parent === document.body) break;
            scope = parent;
            if ((scope.innerText || '').length > 200) break;
        }
        const lines = (scope.innerText || '').split('\\n')
            .map(s => s.trim()).filter(Boolean).slice(0, 12);
        cards.push({profile_url: href, link_text: (link.innerText || '').trim(), lines});
        if (cards.length >= 10) break;
    }
    return cards;
}"""

PLACE_RE = re.compile(r"^.{2,90}$")
_BADGE_RE = re.compile(r"^•")
_BUTTON_LINES = {"follow", "connect", "message", "pending"}


def looks_like_place(line: str, places) -> bool:
    """A location line is comma-separated AND names somewhere inside the
    search lens. Shape alone misfires ("Sales Executive, FMCG" is shaped
    like a place); the lens vocabulary comes from the targets config, so
    this stays data-driven, not hardcoded."""
    text = line.strip()
    if not PLACE_RE.match(text) or "," not in text:
        return False
    lowered = text.lower()
    return any(place in lowered for place in places if place)


def parse_search_card(profile_url: str, link_text: str, lines: list, places=()) -> dict:
    """Turn one card's ordered text lines into a candidate. Name comes from
    the profile link (cut at the • degree badge); headline is the next line;
    location is the line after that when it names somewhere inside the
    search lens. A thin card (location where the headline should be, then a
    Follow/Connect button) is detected and the headline left empty so the
    profile-view enrichment can fill it."""
    texts = [str(line).strip() for line in (lines or []) if str(line).strip()]
    name = link_text.split("•")[0].strip() if link_text else ""
    if not name and texts:
        name = texts[0].split("•")[0].strip()
        texts = texts[1:]
    elif name and texts and (texts[0] == name or texts[0].startswith(name + " ") or "•" in texts[0][: len(name) + 4]):
        # The scope's first line is the name line itself ("Name", "Name • 2nd").
        texts = texts[1:]
    rest = [line for line in texts if not _BADGE_RE.match(line)]
    headline = rest[0] if rest else ""
    location = ""
    if rest:
        if len(rest) > 1 and looks_like_place(rest[0], places) and rest[1].lower() in _BUTTON_LINES:
            location, headline = rest[0], ""
        elif len(rest) > 1 and looks_like_place(rest[1], places):
            location = rest[1]
    return {"profile_url": profile_url, "name": name, "headline": headline, "location": location}


def classify_interstitial(url: str, title: str, body: str) -> InviteStop | None:
    """Map an observed page state to a fatal stop signal, or None when clear.

    Pure function over strings so the stop conditions are unit-testable
    without a browser. Fails closed: callers treat "unknown but not the
    expected invite UI" as UnexpectedUI at the send step; this classifier
    covers the recognizable danger states.
    """
    url, title, body = url or "", title or "", body or ""
    blob = f"{title}\n{body}".lower()
    if _CHALLENGE_URL.search(url) or "verify it's you" in blob or "security verification" in blob:
        return VerificationPrompt(f"challenge at {url}")
    if "captcha" in blob or "recaptcha" in blob or "press & hold" in blob:
        return CaptchaChallenge(f"captcha at {url}")
    if _AUTHWALL_URL.search(url) or ("sign in" in blob and "password" in blob):
        return LoginChallenge(f"auth wall at {url}")
    if (
        "invitation limit" in blob
        or "you've reached" in blob
        or "weekly limit" in blob
        or "try again later" in blob
        or "http 429" in blob
        or "http 999" in blob
    ):
        return RateLimited(f"rate limit at {url}")
    return None


def canonical_key(profile_url: str) -> str:
    """Stable ledger key for a person: the /in/<slug>, lowercased.

    Falls back to the normalized URL (no query/fragment/trailing slash) so a
    non-profile URL still dedupes instead of colliding with everyone else.
    """
    if not profile_url or not profile_url.strip():
        raise ValueError("candidate needs a profile_url")
    parsed = urlparse(profile_url.strip())
    parts = [p for p in parsed.path.split("/") if p]
    slug = ""
    if "in" in parts:
        slug = parts[parts.index("in") + 1 :][:1]
        slug = slug[0] if slug else ""
    slug = re.sub(r"[^a-z0-9-]", "", slug.lower())
    if slug:
        return f"in:{slug}"
    norm = f"{parsed.netloc.lower()}{parsed.path.rstrip('/') or '/'}"
    if not norm.strip("/"):
        raise ValueError(f"cannot key candidate URL: {profile_url!r}")
    return f"url:{norm}"


def parse_window(spec: str) -> tuple[int, int]:
    """Parse "HH:MM-HH:MM" into (start, end) minutes-since-midnight."""
    match = re.fullmatch(r"(\d{2}):(\d{2})-(\d{2}):(\d{2})", (spec or "").strip())
    if not match:
        raise ValueError(f'window must be "HH:MM-HH:MM", got {spec!r}')
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    if not (0 <= int(match.group(1)) < 24 and 0 <= int(match.group(2)) < 60):
        raise ValueError(f"window start out of range: {spec!r}")
    if not (0 <= int(match.group(3)) < 24 and 0 <= int(match.group(4)) < 60):
        raise ValueError(f"window end out of range: {spec!r}")
    return start, end


def inside_window(spec: str, now: datetime | None = None) -> bool:
    """True when local time falls inside the window (overnight spans allowed)."""
    start, end = parse_window(spec)
    current = (now or datetime.now().astimezone()).hour * 60 + (now or datetime.now().astimezone()).minute
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def load_targets(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"cannot read targets file {path}: {exc}")
    if not isinstance(cfg, dict):
        sys.exit(f"targets file {path} must be a JSON object")
    return cfg


def score_candidate(candidate: dict, cfg: dict) -> tuple[float, list[str]]:
    """Score a candidate against the targeting config.

    Criteria are data (role signals, domain signals, location lens), never
    hardcoded names: the same candidate scores differently under a different
    --targets file, which the tests pin.
    """
    headline = str(candidate.get("headline") or "")
    company = str(candidate.get("company") or "")
    title = str(candidate.get("title") or "")
    haystack = f"{title} {headline} {company}".lower()
    location = str(candidate.get("location") or "").lower()

    score = 0.0
    reasons: list[str] = []
    for sig in cfg.get("role_signals", []) or []:
        match = str(sig.get("match") or "").lower()
        if match and match in haystack:
            weight = float(sig.get("weight", 0))
            score += weight
            reasons.append(f"role:{sig.get('tag') or match}({weight:g})")
    domain_weight = float(cfg.get("domain_weight", 1))
    for keyword in cfg.get("domain_signals", []) or []:
        word = str(keyword).lower()
        if word and word in haystack:
            score += domain_weight
            reasons.append(f"domain:{keyword}")
    location_weight = float(cfg.get("location_weight", 1))
    for place in cfg.get("location_allow", []) or []:
        area = str(place).lower()
        if area and area in location:
            score += location_weight
            reasons.append(f"location:{place}")
            break
    return score, reasons


def blank_ledger() -> dict:
    return {"persons": {}, "sends": []}


def load_ledger(path: Path) -> dict:
    if not path.is_file():
        return blank_ledger()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict):
        sys.exit(f"{path}: expected a JSON object with persons/sends")
    data.setdefault("persons", {})
    data.setdefault("sends", [])
    if not isinstance(data["persons"], dict) or not isinstance(data["sends"], list):
        sys.exit(f"{path}: expected persons to be an object and sends to be a list")
    return data


def save_ledger(path: Path, ledger: dict) -> None:
    """Atomic replace: a half-written ledger loses the invite history the
    daily cap and the dedupe both depend on."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ledger.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def sends_today(ledger: dict, day: str) -> int:
    return sum(1 for ts in ledger.get("sends", []) if str(ts)[:10] == day)


def append_audit(path: Path, record: dict) -> None:
    """Append-only: the audit log is never rewritten, only extended."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = scrub(json.dumps(record, ensure_ascii=False))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def effective_cap(max_per_day: int) -> int:
    """--max-per-day can only lower the hard cap, never raise it."""
    if max_per_day < 1:
        raise ValueError("--max-per-day must be at least 1")
    if max_per_day > HARD_DAILY_CAP:
        raise ValueError(
            f"--max-per-day {max_per_day} exceeds the hard daily cap of {HARD_DAILY_CAP} - refusing"
        )
    return min(max_per_day, HARD_DAILY_CAP)


def check_note(note: str | None) -> str | None:
    if note is None:
        return None
    text = note.strip()
    if len(text) > NOTE_LIMIT:
        raise ValueError(f"invite note is {len(text)} chars; LinkedIn allows {NOTE_LIMIT}")
    return text or None


class InviteDriver:
    """Interface live sending is written against. Tests inject fakes."""

    def start(self) -> None:
        raise NotImplementedError

    def send_invite(self, candidate: dict, note: str | None) -> str:
        """Send one invite. Returns "sent", or a terminal outcome such as
        "already_pending" / "already_connected". Raises InviteStop on any
        challenge, captcha, verification, rate-limit, or unexpected UI."""
        raise NotImplementedError

    def search_people(self, keywords: str, location: str, page: int) -> list[dict]:
        """One LinkedIn people-search page. Returns card dicts with
        profile_url/name/headline/location (fields may be empty). Raises
        InviteStop on any challenge, captcha, verification, rate-limit, or
        unexpected UI. No retries."""
        raise NotImplementedError

    def view_profile(self, profile_url: str) -> dict:
        """Fetch one profile's headline/location to enrich a search card.
        Raises InviteStop like every other driver call. No retries."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class PlaywrightDriver(InviteDriver):
    """Live LinkedIn driver. Created only for --live runs; the import stays
    lazy so dry runs, planning, and tests never need Playwright installed."""

    def __init__(self, profile_dir: Path, places=()):
        self.profile_dir = profile_dir
        self._places = [str(p).lower() for p in places or [] if str(p).strip()]
        self._pw = None
        self._ctx = None

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            sys.exit("live sending needs Playwright: pip install playwright && python -m playwright install chromium")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            str(self.profile_dir), headless=False, viewport={"width": 1280, "height": 900}
        )

    def _page_state(self, page) -> InviteStop | None:
        try:
            body = page.evaluate("() => document.body ? document.body.innerText.slice(0, 4000) : ''")
        except Exception:
            body = ""
        return classify_interstitial(page.url, page.title(), body or "")

    def send_invite(self, candidate: dict, note: str | None) -> str:
        page = self._ctx.new_page()
        try:
            try:
                page.goto(candidate["profile_url"], wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)
            except Exception as exc:
                raise UnexpectedUI(f"profile page did not load: {exc}") from exc
            stop = self._page_state(page)
            if stop is not None:
                raise stop
            # Already-contacted states are recorded, never re-sent.
            pending = page.get_by_text("Pending", exact=False).first
            try:
                if pending.count() > 0:
                    return "already_pending"
            except Exception as exc:
                raise UnexpectedUI(f"could not read connection badge: {exc}") from exc
            try:
                message = page.get_by_role("button", name=re.compile(r"^Message", re.I)).first
                if message.count() > 0:
                    return "already_connected"
            except Exception as exc:
                raise UnexpectedUI(f"could not read message button: {exc}") from exc
            # Card-scoped, normalized-name matching. The card is anchored on
            # its Contact-info link (a UI staple), so a suggested profile's
            # Connect elsewhere on the page can never be mistaken for this
            # profile's - clicking that would invite a stranger. Names come
            # from aria-labels with innerText fallback, then normalized
            # ("+ Follow" -> "Follow"); "Invite X to connect" never
            # collapses onto "Connect". A Connect hidden under More is
            # deliberately NOT pursued: its menu item cannot be identified
            # without guessing, and guessing is what broke this.
            anchor = page.get_by_text("Contact info", exact=True).first
            try:
                has_anchor = anchor.count() > 0
            except Exception as exc:
                raise UnexpectedUI(f"could not read top-card actions: {exc}") from exc
            if not has_anchor:
                raise UnexpectedUI("profile top card has no Contact-info anchor")
            try:
                card = anchor.locator("xpath=./ancestor-or-self::*[.//button][1]")
                raw_names = card.get_by_role("button").evaluate_all(
                    "els => els.map(e => e.getAttribute('aria-label') || e.innerText || '')"
                )
                try:
                    card_text = card.evaluate("(el) => el.innerText || ''")
                except Exception:
                    card_text = ""
            except Exception as exc:
                raise UnexpectedUI(f"could not read top-card actions: {exc}") from exc
            acc_entry = classify_invite_entry(raw_names or [])
            text_entry = classify_entry_from_texts((card_text or "").splitlines())
            entry = resolve_entry(acc_entry, text_entry)
            if entry == "follow_only":
                # Creator-mode profiles accept invites via More -> Connect.
                return self._more_connect_flow(page, card, candidate, note)
            if entry == "unknown":
                return UNKNOWN_CONTROLS
            connect = card.get_by_role("button", name=re.compile(r"^\W*Connect\s*$", re.I)).first
            try:
                connect.click(timeout=10000)
                page.wait_for_timeout(1500)
            except InviteStop:
                raise
            except Exception as exc:
                raise UnexpectedUI(f"Connect control did not behave: {exc}") from exc
            return self._send_bare_from_dialog(page, note)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def close(self) -> None:
        for handle in (self._ctx, self._pw):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass
        self._ctx, self._pw = None, None

    def _more_connect_flow(self, page, card, candidate: dict, note: str | None) -> str:
        """Follow-only entry: open More, choose the verified Connect item,
        send bare. Returns "sent", FOLLOW_ONLY (no usable entry), or
        NOTE_REQUIRED (dialog forces a note - never written). Mechanical
        failures map to UNKNOWN_CONTROLS so the storm guard watches them;
        the challenge family still raises."""
        try:
            more = card.get_by_role("button", name=re.compile(r"^\W*More\s*$", re.I)).first
            if more.count() == 0:
                return FOLLOW_ONLY
            more.click(timeout=10000)
            page.wait_for_timeout(1500)
        except InviteStop:
            raise
        except Exception:
            return UNKNOWN_CONTROLS
        stop = self._page_state(page)
        if stop is not None:
            raise stop
        try:
            menu_names = page.evaluate(
                """() => Array.from(document.querySelectorAll(
                    '[role="menu"] [role="menuitem"], [role="menu"] button, '
                    '[role="menu"] a, [role="dialog"] button'
                )).map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())"""
            )
        except Exception:
            return UNKNOWN_CONTROLS
        entry_name = pick_invite_entry(menu_names or [], candidate.get("name") or "")
        if entry_name is None:
            return FOLLOW_ONLY
        try:
            page.locator(
                'button, a, [role="button"], [role="menuitem"], [role="menuitemcheckbox"], li'
            ).get_by_text(entry_name, exact=True).first.click(timeout=10000)
            page.wait_for_timeout(1500)
        except InviteStop:
            raise
        except Exception:
            return UNKNOWN_CONTROLS
        return self._send_bare_from_dialog(page, note)

    def _send_bare_from_dialog(self, page, note: str | None) -> str:
        """Complete an opened invite dialog with a bare Send. A dialog that
        offers no Send but demands a note reports NOTE_REQUIRED - a note is
        never written, no exceptions. Anything else unrecognized raises."""
        stop = self._page_state(page)
        if stop is not None:
            raise stop
        dialog_text = (page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
        if "invitation limit" in dialog_text or "you've reached" in dialog_text:
            raise RateLimited("invite-limit banner after pressing Connect")
        if note:
            try:
                add_note = page.get_by_role("button", name=re.compile(r"Add a note", re.I)).first
                if add_note.count() > 0:
                    add_note.click(timeout=10000)
                    box = page.get_by_role("textbox").first
                    box.fill(note[:NOTE_LIMIT], timeout=10000)
            except InviteStop:
                raise
            except Exception as exc:
                raise UnexpectedUI(f"note dialog did not behave: {exc}") from exc
        try:
            send = page.get_by_role("button", name=re.compile(r"^Send", re.I)).first
            if send.count() > 0:
                send.click(timeout=10000)
                page.wait_for_timeout(1500)
            else:
                try:
                    textbox = page.get_by_role("textbox").first
                    has_box = textbox.count() > 0
                except Exception:
                    has_box = False
                if has_box:
                    return NOTE_REQUIRED
                raise UnexpectedUI("invite dialog has no Send button")
        except Exception as exc:
            raise UnexpectedUI(f"Send button did not behave: {exc}") from exc
        stop = self._page_state(page)
        if stop is not None:
            raise stop
        return "sent"

    def _checked_goto(self, url: str, candidate: str) -> object:
        """Open a page and fail closed on anything but the expected UI."""
        page = self._ctx.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)
            except Exception as exc:
                page.close()
                raise UnexpectedUI(f"{candidate} page did not load: {exc}") from exc
            stop = self._page_state(page)
            if stop is not None:
                page.close()
                raise stop
            return page
        except InviteStop:
            raise
        except Exception as exc:
            try:
                page.close()
            except Exception:
                pass
            raise UnexpectedUI(f"{candidate} page did not behave: {exc}") from exc

    def search_people(self, keywords: str, location: str, page: int) -> list[dict]:
        import urllib.parse

        params = urllib.parse.urlencode({"keywords": keywords, "origin": "GLOBAL_SEARCH_HEADER", "page": page})
        url = f"https://www.linkedin.com/search/results/people/?{params}"
        driver_page = self._checked_goto(url, "people-search")
        try:
            try:
                raw = driver_page.evaluate(SEARCH_CARDS_JS)
            except Exception as exc:
                raise UnexpectedUI(f"could not read search results: {exc}") from exc
            cards = []
            for entry in raw or []:
                if not isinstance(entry, dict) or not entry.get("profile_url"):
                    continue
                try:
                    cards.append(
                        parse_search_card(
                            entry["profile_url"],
                            entry.get("link_text") or "",
                            entry.get("lines") or [],
                            self._places,
                        )
                    )
                except Exception as exc:
                    raise UnexpectedUI(f"could not parse a search card: {exc}") from exc
            if not cards:
                body = (driver_page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
                if "no results" in body or "no matches" in body or "0 results" in body:
                    return []
                raise UnexpectedUI("people-search page has no result cards and no empty-state message")
            return cards
        finally:
            try:
                driver_page.close()
            except Exception:
                pass

    def view_profile(self, profile_url: str) -> dict:
        driver_page = self._checked_goto(profile_url, "profile")
        try:
            try:
                data = driver_page.evaluate(
                    """() => {
                        const pick = (sels) => {
                            for (const sel of sels) {
                                const el = document.querySelector(sel);
                                if (el && el.innerText.trim()) return el.innerText.trim();
                            }
                            return '';
                        };
                        return {
                            headline: pick([
                                '.text-body-medium.break-words',
                                '.pv-text-details__left-panel .text-body-medium',
                            ]),
                            location: pick([
                                '.text-body-small.inline.t-black--light.break-words',
                                '.pv-text-details__left-panel .text-body-small',
                            ]),
                        };
                    }"""
                )
            except Exception as exc:
                raise UnexpectedUI(f"could not read profile header: {exc}") from exc
            if not isinstance(data, dict) or not (data.get("headline") or data.get("location")):
                raise UnexpectedUI("profile header has neither headline nor location")
            return {"headline": data.get("headline") or "", "location": data.get("location") or ""}
        finally:
            try:
                driver_page.close()
            except Exception:
                pass


CACHE_NAME = "discovery_cache.json"


def discovery_defaults(cfg: dict) -> dict:
    """Budgets and seeds for discover, from the targets file's discovery
    section. Low by design: discovery traffic is what gets accounts flagged."""
    section = cfg.get("discovery", {}) or {}
    if not isinstance(section, dict):
        sys.exit("targets file: discovery must be an object")
    return {
        "max_searches": int(section.get("max_searches", 3)),
        "max_pages": int(section.get("max_pages", 2)),
        "max_profile_views": int(section.get("max_profile_views", 5)),
        "max_queued_per_day": int(section.get("max_queued_per_day", HARD_DAILY_CAP)),
        "search_min_delay": float(section.get("search_min_delay", 8.0)),
        "search_max_delay": float(section.get("search_max_delay", 20.0)),
        "role_seeds": section.get("role_seeds", []) or [],
        "locations": section.get("locations", []) or [],
    }


def discovery_queries(cfg: dict) -> list[dict]:
    """Seeded discovery queries: role-title seeds x the GenAI domain terms
    already in this targets file x the location lens. Configurable data, not
    hardcoded names: reordering the seeds reprioritises the searches, and
    execution stops at max_searches, so listed order is priority order. No
    company-list mining and no pasted URLs in v1 - these seeds are the only
    discovery source."""
    defaults = discovery_defaults(cfg)
    domains = [str(d).strip() for d in cfg.get("domain_signals", []) or [] if str(d).strip()]
    roles = [str(r).strip() for r in defaults["role_seeds"] if str(r).strip()]
    locations = [str(loc).strip() for loc in defaults["locations"] if str(loc).strip()]
    queries = []
    for domain in domains or [""]:
        for location in locations or [""]:
            for role in roles:
                keywords = " ".join(part for part in (role, domain, location) if part)
                queries.append({"keywords": keywords, "role": role, "domain": domain, "location": location})
    return queries


def queued_today(ledger: dict, day: str) -> int:
    """Persons newly queued by discovery today - the discovery-side cap that
    mirrors the invite cap."""
    count = 0
    for person in (ledger.get("persons", {}) or {}).values():
        if person.get("status") == "queued" and str(person.get("last_ts") or "")[:10] == day:
            count += 1
    return count


def load_cache(path: Path) -> dict:
    """Discovery cache: raw search pages keyed by query+location+page, so a
    re-run re-scores from disk and never re-scrapes LinkedIn."""
    if not path.is_file():
        return {"version": 1, "pages": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("pages"), dict):
        sys.exit(f"{path}: expected a cache object with a pages map")
    return data


def save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".discovery.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cache_key(keywords: str, location: str, page: int) -> str:
    return f"{keywords}\n{location}\n{page}"


def pace_delay(min_delay: float, max_delay: float, rng: random.Random) -> float:
    """Jittered pause between invites. Uniform in [min, max]; a zero window
    is allowed by tests, never by the CLI defaults."""
    if min_delay < 0 or max_delay < min_delay:
        raise ValueError(f"bad delay window [{min_delay}, {max_delay}]")
    return rng.uniform(min_delay, max_delay)


def run_queue(
    candidates: list[dict],
    cfg: dict,
    ledger: dict,
    audit_path: Path,
    *,
    live: bool,
    cap: int,
    day: str,
    window: str,
    note: str | None,
    limit: int,
    min_delay: float,
    max_delay: float,
    driver_factory=None,
    sleeper=None,
    rng: random.Random | None = None,
    now_iso: str | None = None,
) -> dict:
    """Execute one bot pass. Returns a summary dict; raises InviteStop on a
    safety signal and SystemExit(2) is left to the CLI wrapper.

    Dry-run (live=False) never creates a driver and never mutates the ledger:
    it logs exactly what WOULD be sent. Live runs mutate the ledger only
    after an outcome is known, and stop at the first InviteStop with no
    retry of the failed - or any later - candidate. Per-profile unknown
    controls skip against a storm budget instead: the 4th consecutive
    unknown halts the run loud (UI drift must be noticed, not plowed
    through), while anything else decided in between resets the count.
    """
    rng = rng or random.Random()
    timestamp = now_iso or utcnow_iso()
    summary: dict = {
        "live": live,
        "day": day,
        "cap": cap,
        "sent": 0,
        "would_send": 0,
        "skipped": 0,
        "unknown_skips": 0,
        "stopped": None,
        "decisions": [],
    }

    if not inside_window(window):
        summary["stopped"] = "outside_window"
        append_audit(
            audit_path,
            {"ts": timestamp, "decision": "refuse", "reason": f"outside daily window {window}", "live": live},
        )
        return summary

    min_score = float(cfg.get("min_score", 0))
    driver = None
    if live:
        if driver_factory is None:
            raise ValueError("live run needs a driver factory")
        driver = driver_factory()
        driver.start()

    sent_this_run = 0
    unknown_streak = 0
    try:
        for candidate in candidates:
            if limit > 0 and (summary["sent"] + summary["would_send"] + summary["skipped"]) >= limit:
                break
            try:
                key = canonical_key(str(candidate.get("profile_url") or ""))
            except ValueError as exc:
                unknown_streak = 0
                summary["skipped"] += 1
                record = {"ts": timestamp, "decision": "skip", "reason": f"unkeyable: {exc}", "live": live}
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                continue
            known = (ledger.get("persons", {}) or {}).get(key)
            if known and known.get("status") in RUN_SKIP_STATUSES:
                unknown_streak = 0
                summary["skipped"] += 1
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "skip", "reason": f"dedupe: already {known['status']}", "live": live,
                }
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                continue
            score, reasons = score_candidate(candidate, cfg)
            if score < min_score:
                unknown_streak = 0
                summary["skipped"] += 1
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "skip", "reason": f"below threshold ({score:g} < {min_score:g})",
                    "score": score, "live": live,
                }
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                continue
            # sends_today reads the ledger, which already includes this run's
            # sends (appended on each outcome) - adding sent_this_run on top
            # would double-count and halt one invite early.
            if sends_today(ledger, day) >= cap:
                summary["stopped"] = "cap_reached"
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "defer", "reason": f"daily cap reached ({cap})", "live": live,
                }
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                break
            if not live:
                unknown_streak = 0
                summary["would_send"] += 1
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "dry-run", "reason": "; ".join(reasons),
                    "score": score, "live": False,
                }
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                continue
            if sent_this_run > 0:
                delay = pace_delay(min_delay, max_delay, rng)
                (sleeper or (lambda s: __import__("time").sleep(s)))(delay)
            try:
                outcome = driver.send_invite(candidate, note)
            except InviteStop as stop:
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "stop", "reason": f"{stop.signal}: {stop}", "live": True,
                }
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                summary["stopped"] = stop.signal
                raise
            prev = (ledger.get("persons", {}) or {}).get(key, {})
            person = {
                "status": None,
                "last_ts": timestamp,
                "score": score,
                "reasons": reasons,
                "profile_url": candidate.get("profile_url") or prev.get("profile_url"),
                "name": candidate.get("name") or prev.get("name"),
                # A run must never strip the discovery snapshot: re-scoring
                # a run-touched profile reads these fields, and blanking
                # them silently drops it below threshold forever.
                "headline": candidate.get("headline") or prev.get("headline"),
                "location": candidate.get("location") or prev.get("location"),
                "company": candidate.get("company") or prev.get("company"),
            }
            if outcome == UNKNOWN_CONTROLS:
                # No ledger write: the profile stays queued for a later run,
                # while the per-run storm guard watches for UI drift.
                unknown_streak += 1
                summary["skipped"] += 1
                summary["unknown_skips"] += 1
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "skip", "result": outcome,
                    "reason": f"unknown controls, {unknown_streak} consecutive",
                    "score": score, "live": True,
                }
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                if unknown_streak > MAX_CONSECUTIVE_UNKNOWN:
                    summary["stopped"] = "unknown_controls"
                    break
                continue
            unknown_streak = 0
            if outcome == "sent":
                person["status"] = "invited"
                ledger["sends"].append(timestamp)
                sent_this_run += 1
                summary["sent"] += 1
            elif outcome == "already_pending":
                person["status"] = "invited"
            elif outcome == "already_connected":
                person["status"] = "connected"
            elif outcome == FOLLOW_ONLY:
                person["status"] = FOLLOW_ONLY
            elif outcome == NOTE_REQUIRED:
                person["status"] = NOTE_REQUIRED
            else:
                person["status"] = str(outcome)
            ledger.setdefault("persons", {})[key] = person
            if outcome == FOLLOW_ONLY:
                summary["skipped"] += 1
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "skip", "result": outcome,
                    "reason": "follow-only profile (creator mode): no usable Connect entry, never force-clicked",
                    "score": score, "live": True,
                }
            elif outcome == NOTE_REQUIRED:
                summary["skipped"] += 1
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "skip", "result": outcome,
                    "reason": "dialog forces a note: bare send impossible, and no note is ever written",
                    "score": score, "live": True,
                }
            else:
                record = {
                    "ts": timestamp, "profile": key, "name": candidate.get("name"),
                    "decision": "send", "result": outcome, "reason": "; ".join(reasons),
                    "score": score, "live": True,
                }
            append_audit(audit_path, record)
            summary["decisions"].append(record)
    finally:
        if driver is not None:
            driver.close()
    return summary


def run_discover(
    queries: list[dict],
    cfg: dict,
    ledger: dict,
    cache: dict,
    audit_path: Path,
    *,
    live: bool,
    day: str,
    window: str,
    max_searches: int,
    max_pages: int,
    max_profile_views: int,
    max_queued: int,
    search_min_delay: float,
    search_max_delay: float,
    driver_factory=None,
    sleeper=None,
    rng: random.Random | None = None,
    now_iso: str | None = None,
) -> dict:
    """Execute one discovery pass: search people-search, score the cards with
    the invite matcher's score_candidate, and queue matches in the ledger for
    a later run to invite. Returns a summary; raises InviteStop on any safety
    signal.

    Dry-run (live=False) is read-only: it reads the cache, scores what is
    cached, and prints what it WOULD search and WOULD queue. It writes no
    cache, no ledger, and no audit entries. Live runs append raw pages to
    the cache, mark matches queued in the ledger, and audit every decision.
    """
    rng = rng or random.Random()
    timestamp = now_iso or utcnow_iso()
    summary: dict = {
        "live": live,
        "day": day,
        "searches_executed": 0,
        "pages_fetched": 0,
        "profile_views": 0,
        "cache_hits": 0,
        "queued": 0,
        "skipped": 0,
        "deferred": 0,
        "stopped": None,
        "decisions": [],
    }

    if not inside_window(window):
        summary["stopped"] = "outside_window"
        return summary

    min_score = float(cfg.get("min_score", 0))
    driver = None
    if live:
        if driver_factory is None:
            raise ValueError("live discovery needs a driver factory")
        driver = driver_factory()
        driver.start()

    def audit(record: dict) -> None:
        if live:
            append_audit(audit_path, record)

    fetches = 0
    try:
        for query in queries[: max(0, max_searches)]:
            keywords = query["keywords"]
            location = query.get("location") or ""
            for page in range(1, max(0, max_pages) + 1):
                key = cache_key(keywords, location, page)
                if key in (cache.get("pages", {}) or {}):
                    results = cache["pages"][key].get("results", [])
                    summary["cache_hits"] += 1
                elif not live:
                    record = {
                        "ts": timestamp, "decision": "would-search",
                        "keywords": keywords, "location": location, "page": page, "live": False,
                    }
                    summary["decisions"].append(record)
                    break
                else:
                    if fetches > 0:
                        delay = pace_delay(search_min_delay, search_max_delay, rng)
                        (sleeper or (lambda s: __import__("time").sleep(s)))(delay)
                    try:
                        results = driver.search_people(keywords, location, page)
                    except InviteStop as stop:
                        record = {
                            "ts": timestamp, "decision": "stop",
                            "reason": f"{stop.signal}: {stop}",
                            "keywords": keywords, "live": True,
                        }
                        audit(record)
                        summary["decisions"].append(record)
                        summary["stopped"] = stop.signal
                        raise
                    fetches += 1
                    summary["searches_executed"] += 1
                    summary["pages_fetched"] += 1
                    cache.setdefault("pages", {})[key] = {"ts": timestamp, "results": results}
                    if not results:
                        break
                for card in results:
                    summary["decisions"].append(
                        handle_card(
                            card, query, cfg, ledger, min_score, live, timestamp,
                            day, max_queued, max_profile_views, driver, summary, audit,
                        )
                    )
                    if summary["stopped"] == "queue_cap_reached":
                        break
                if summary["stopped"] == "queue_cap_reached":
                    break
        summary["deferred"] = max(0, len(queries) - max(0, max_searches))
    finally:
        if driver is not None:
            driver.close()
    return summary


def handle_card(
    card: dict,
    query: dict,
    cfg: dict,
    ledger: dict,
    min_score: float,
    live: bool,
    timestamp: str,
    day: str,
    max_queued: int,
    max_profile_views: int,
    driver,
    summary: dict,
    audit,
) -> dict:
    """Score one search card and queue, skip, or defer it. Returns the
    decision record (also appended to summary counts by the caller)."""
    candidate = {
        "profile_url": card.get("profile_url") or "",
        "name": card.get("name") or "",
        "headline": card.get("headline") or "",
        "location": card.get("location") or "",
    }
    try:
        key = canonical_key(candidate["profile_url"])
    except ValueError as exc:
        summary["skipped"] += 1
        record = {"ts": timestamp, "decision": "skip", "reason": f"unkeyable card: {exc}", "live": live}
        audit(record)
        return record
    known = (ledger.get("persons", {}) or {}).get(key)
    if known and known.get("status") in DISCOVERY_STATUSES:
        summary["skipped"] += 1
        record = {
            "ts": timestamp, "profile": key, "name": candidate["name"],
            "decision": "skip", "reason": f"dedupe: already {known['status']}", "live": live,
        }
        audit(record)
        return record
    # A card without a headline cannot be scored fairly - the signal usually
    # lives in the headline - so thin cards earn a profile view (while the
    # view budget lasts) before the threshold check, not after.
    if live and not candidate["headline"] and summary["profile_views"] < max(0, max_profile_views):
        try:
            enriched = driver.view_profile(candidate["profile_url"])
        except InviteStop as stop:
            record = {
                "ts": timestamp, "profile": key, "name": candidate["name"],
                "decision": "stop", "reason": f"{stop.signal}: {stop}", "live": True,
            }
            audit(record)
            summary["decisions"].append(record)
            summary["stopped"] = stop.signal
            raise
        summary["profile_views"] += 1
        candidate["headline"] = enriched.get("headline") or candidate["headline"]
        candidate["location"] = enriched.get("location") or candidate["location"]
    score, reasons = score_candidate(candidate, cfg)
    if score < min_score:
        summary["skipped"] += 1
        record = {
            "ts": timestamp, "profile": key, "name": candidate["name"],
            "decision": "skip", "reason": f"below threshold ({score:g} < {min_score:g})",
            "score": score, "live": live,
        }
        audit(record)
        return record
    if not live:
        summary["queued"] += 1
        return {
            "ts": timestamp, "profile": key, "name": candidate["name"],
            "decision": "would-queue", "reason": "; ".join(reasons),
            "score": score, "live": False,
        }
    if queued_today(ledger, day) >= max(1, max_queued):
        summary["stopped"] = "queue_cap_reached"
        return {
            "ts": timestamp, "profile": key, "name": candidate["name"],
            "decision": "defer", "reason": f"daily queue cap reached ({max_queued})", "live": True,
        }
    ledger.setdefault("persons", {})[key] = {
        "status": "queued",
        "last_ts": timestamp,
        "score": score,
        "reasons": reasons,
        "profile_url": candidate["profile_url"],
        "name": candidate["name"],
        "headline": candidate["headline"],
        "location": candidate["location"],
    }
    summary["queued"] += 1
    record = {
        "ts": timestamp, "profile": key, "name": candidate["name"],
        "decision": "queue", "reason": "; ".join(reasons),
        "score": score, "keywords": query["keywords"], "live": True,
    }
    audit(record)
    return record


def load_candidates(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"cannot read candidates file {path}: {exc}")
    if isinstance(data, dict):
        data = data.get("candidates", [])
    if not isinstance(data, list) or not all(isinstance(c, dict) for c in data):
        sys.exit(f"candidates file {path} must be a JSON array of objects")
    return data


def cmd_discover(args) -> int:
    cfg = load_targets(args.targets)
    defaults = discovery_defaults(cfg)
    max_searches = args.max_searches if args.max_searches is not None else defaults["max_searches"]
    max_pages = args.max_pages if args.max_pages is not None else defaults["max_pages"]
    max_views = args.max_profile_views if args.max_profile_views is not None else defaults["max_profile_views"]
    max_queued = args.max_queued if args.max_queued is not None else defaults["max_queued_per_day"]
    search_min = args.search_min_delay if args.search_min_delay is not None else defaults["search_min_delay"]
    search_max = args.search_max_delay if args.search_max_delay is not None else defaults["search_max_delay"]
    for name, value in [("max_searches", max_searches), ("max_pages", max_pages),
                        ("max_profile_views", max_views), ("max_queued", max_queued)]:
        if value < 1:
            sys.exit(f"--{name.replace('_', '-')} must be at least 1")
    if max_queued > HARD_DAILY_CAP:
        sys.exit(f"--max-queued {max_queued} exceeds the hard daily cap of {HARD_DAILY_CAP} - refusing")
    if search_min < 0 or search_max < search_min:
        sys.exit(f"bad search delay window [{search_min}, {search_max}]")
    try:
        parse_window(args.window)
    except ValueError as exc:
        sys.exit(str(exc))
    queries = discovery_queries(cfg)
    if not queries:
        sys.exit("targets file: discovery has no role_seeds to search from")
    ledger_path = args.state_dir / LEDGER_NAME
    cache_path = args.state_dir / CACHE_NAME
    audit_path = args.state_dir / AUDIT_NAME
    ledger = load_ledger(ledger_path)
    cache = load_cache(cache_path)
    day = today_str()

    def factory():
        return PlaywrightDriver(args.profile_dir, places=cfg.get("location_allow", []))

    try:
        summary = run_discover(
            queries, cfg, ledger, cache, audit_path, live=args.live, day=day,
            window=args.window, max_searches=max_searches, max_pages=max_pages,
            max_profile_views=max_views, max_queued=max_queued,
            search_min_delay=search_min, search_max_delay=search_max,
            driver_factory=factory,
        )
    except InviteStop as stop:
        save_ledger(ledger_path, ledger)
        save_cache(cache_path, cache)
        print(f"BOT STOPPED - {stop.loud}: {stop}", file=sys.stderr)
        print(f"Ledger and cache saved. No further searches attempted. Audit: {audit_path}", file=sys.stderr)
        return EXIT_STOPPED

    if args.live:
        save_ledger(ledger_path, ledger)
        save_cache(cache_path, cache)
        print(f"live discovery: {summary['queued']} queued, {summary['skipped']} skipped, "
              f"{summary['searches_executed']} searches, {summary['profile_views']} profile views, "
              f"{summary['cache_hits']} cache hits.")
    else:
        for row in summary["decisions"]:
            if row["decision"] == "would-search":
                print(f"WOULD SEARCH people for {row['keywords']!r} ({row['location'] or 'anywhere'})")
            elif row["decision"] == "would-queue":
                print(f"WOULD QUEUE {row.get('name') or row['profile']} [{row['profile']}] ({row['reason']})")
        print(f"dry-run: {summary['queued']} would-queue, {summary['skipped']} skipped - "
              "nothing searched, nothing queued, nothing written.")
    if summary["stopped"] == "queue_cap_reached":
        print(f"daily queue cap reached ({max_queued}) - further matches deferred to tomorrow.")
    if summary["stopped"] == "outside_window":
        print(f"refused: outside the daily window {args.window} - nothing searched.", file=sys.stderr)
        return 1
    if summary["deferred"]:
        print(f"{summary['deferred']} lower-priority seed queries deferred by the search budget.")
    return 0


def cmd_plan(args) -> int:
    cfg = load_targets(args.targets)
    candidates = load_candidates(args.candidates)
    ledger = load_ledger(args.state_dir / LEDGER_NAME)
    min_score = float(cfg.get("min_score", 0))
    rows = []
    for candidate in candidates:
        try:
            key = canonical_key(str(candidate.get("profile_url") or ""))
        except ValueError as exc:
            rows.append({"profile": None, "name": candidate.get("name"), "decision": "skip",
                         "reason": f"unkeyable: {exc}"})
            continue
        known = ledger.get("persons", {}).get(key)
        if known and known.get("status") in TERMINAL_STATUSES:
            rows.append({"profile": key, "name": candidate.get("name"), "decision": "skip",
                         "reason": f"dedupe: already {known['status']}"})
            continue
        score, reasons = score_candidate(candidate, cfg)
        if score < min_score:
            rows.append({"profile": key, "name": candidate.get("name"), "decision": "skip",
                         "score": score, "reason": f"below threshold ({score:g} < {min_score:g})"})
        else:
            rows.append({"profile": key, "name": candidate.get("name"), "decision": "invite",
                         "score": score, "reason": "; ".join(reasons)})
    print(json.dumps({"candidates": len(candidates), "rows": rows}, indent=2, ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    try:
        cap = effective_cap(args.max_per_day)
    except ValueError as exc:
        sys.exit(str(exc))
    try:
        parse_window(args.window)
    except ValueError as exc:
        sys.exit(str(exc))
    try:
        note = check_note(Path(args.note_file).read_text(encoding="utf-8") if args.note_file else None)
    except (OSError, ValueError) as exc:
        sys.exit(f"bad note file: {exc}")
    if args.min_delay < 0 or args.max_delay < args.min_delay:
        sys.exit(f"bad delay window [{args.min_delay}, {args.max_delay}]")
    cfg = load_targets(args.targets)
    ledger_path = args.state_dir / LEDGER_NAME
    audit_path = args.state_dir / AUDIT_NAME
    ledger = load_ledger(ledger_path)
    if args.from_ledger:
        candidates = []
        for person in (ledger.get("persons", {}) or {}).values():
            if person.get("status") == "queued" and person.get("profile_url"):
                candidates.append({
                    "profile_url": person["profile_url"],
                    "name": person.get("name") or "",
                    "headline": person.get("headline") or "",
                    "location": person.get("location") or "",
                })
        if args.candidates is not None:
            candidates.extend(load_candidates(args.candidates))
        if not candidates:
            print("no queued profiles in the ledger - run discover first or pass --candidates.")
    elif args.candidates is None:
        sys.exit("pass --candidates queue.json or --from-ledger for discover-queued profiles")
    else:
        candidates = load_candidates(args.candidates)
    day = today_str()

    def factory():
        return PlaywrightDriver(args.profile_dir)

    try:
        summary = run_queue(
            candidates, cfg, ledger, audit_path, live=args.live, cap=cap, day=day,
            window=args.window, note=note, limit=args.limit,
            min_delay=args.min_delay, max_delay=args.max_delay, driver_factory=factory,
        )
    except InviteStop as stop:
        save_ledger(ledger_path, ledger)
        print(f"BOT STOPPED - {stop.loud}: {stop}", file=sys.stderr)
        print(f"Ledger saved. No further invites attempted. Audit: {audit_path}", file=sys.stderr)
        return EXIT_STOPPED

    if args.live:
        save_ledger(ledger_path, ledger)
    if not args.live:
        for row in summary["decisions"]:
            if row["decision"] == "dry-run":
                print(f"WOULD SEND to {row.get('name') or row['profile']} [{row['profile']}] ({row['reason']})")
            else:
                print(f"SKIP {row.get('name') or row.get('profile')}: {row['reason']}")
        print(f"dry-run: {summary['would_send']} would-send, {summary['skipped']} skipped - nothing sent, ledger untouched.")
    else:
        print(f"live: {summary['sent']} sent, {summary['skipped']} skipped.")
    if summary["stopped"] == "cap_reached":
        print(f"daily cap reached ({cap}) - remaining queue deferred to tomorrow.")
    if summary["stopped"] == "unknown_controls":
        print(f"BOT STOPPED - {MAX_CONSECUTIVE_UNKNOWN + 1} consecutive profiles with unreadable "
              f"controls; LinkedIn UI may have drifted. Ledger saved, audit holds every skip.",
              file=sys.stderr)
        return EXIT_STOPPED
    if summary["stopped"] == "outside_window":
        print(f"refused: outside the daily window {args.window} - nothing sent.", file=sys.stderr)
        return 1
    return 0


def cmd_status(args) -> int:
    ledger = load_ledger(args.state_dir / LEDGER_NAME)
    day = today_str()
    try:
        cap = effective_cap(args.max_per_day)
    except ValueError as exc:
        sys.exit(str(exc))
    by_status: dict[str, int] = {}
    for person in (ledger.get("persons") or {}).values():
        status = str(person.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    print(json.dumps({
        "day": day,
        "sent_today": sends_today(ledger, day),
        "cap": cap,
        "remaining_today": max(0, cap - sends_today(ledger, day)),
        "persons_by_status": by_status,
        "ledger": str(args.state_dir / LEDGER_NAME),
        "audit": str(args.state_dir / AUDIT_NAME),
    }, indent=2))
    return 0


def cmd_login(args) -> int:
    """One-time setup: open a persistent browser for manual login.

    The operator signs in by hand inside the opened window (including any
    2FA), then closes it. Session cookies stay in the profile directory -
    this tool never sees, stores, or logs a password or token.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("login needs Playwright: pip install playwright && python -m playwright install chromium")
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    print(f"Opening LinkedIn login in persistent profile {args.profile_dir}")
    print("Sign in by hand (2FA included), then close the window. Nothing is recorded by this tool.")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(args.profile_dir), headless=False)
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        try:
            input("Press Enter here after you are signed in and have closed the browser... ")
        except EOFError:
            pass
        ctx.close()
    print(f"Profile ready at {args.profile_dir} - it stays on this machine and is never committed.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    common.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "linkedin connect bot")
    sub = ap.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", parents=[common], help="score the queue without sending or writing state")
    plan.add_argument("--candidates", type=Path, required=True)
    plan.set_defaults(func=cmd_plan)

    discover = sub.add_parser("discover", parents=[common],
                                      help="find profiles via people search and queue matches (dry-run unless --live)")
    discover.add_argument("--live", action="store_true", help="explicit operator flag: actually search and queue")
    discover.add_argument("--max-searches", type=int, default=None)
    discover.add_argument("--max-pages", type=int, default=None)
    discover.add_argument("--max-profile-views", type=int, default=None)
    discover.add_argument("--max-queued", type=int, default=None)
    discover.add_argument("--search-min-delay", type=float, default=None)
    discover.add_argument("--search-max-delay", type=float, default=None)
    discover.add_argument("--window", default=DEFAULT_WINDOW, help='daily window "HH:MM-HH:MM" local time')
    discover.add_argument("--profile-dir", type=Path,
                          default=Path.home() / ".linkedin-connect-bot" / "profile")
    discover.set_defaults(func=cmd_discover)

    run = sub.add_parser("run", parents=[common], help="run the queue (dry-run unless --live)")
    run.add_argument("--candidates", type=Path, default=None)
    run.add_argument("--from-ledger", action="store_true",
                     help="invite discover-queued profiles from the ledger (composes with --candidates)")
    run.add_argument("--live", action="store_true", help="explicit operator flag: actually send invites")
    run.add_argument("--max-per-day", type=int, default=HARD_DAILY_CAP)
    run.add_argument("--limit", type=int, default=0, help="cap decisions this run (0 = no run cap)")
    run.add_argument("--window", default=DEFAULT_WINDOW, help='daily window "HH:MM-HH:MM" local time')
    run.add_argument("--note-file", type=Path, help="optional invite note file (<=300 chars)")
    run.add_argument("--min-delay", type=float, default=DEFAULT_MIN_DELAY)
    run.add_argument("--max-delay", type=float, default=DEFAULT_MAX_DELAY)
    run.add_argument("--profile-dir", type=Path,
                     default=Path.home() / ".linkedin-connect-bot" / "profile")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common], help="ledger counts and remaining quota")
    status.add_argument("--max-per-day", type=int, default=HARD_DAILY_CAP)
    status.set_defaults(func=cmd_status)

    login = sub.add_parser("login", parents=[common], help="one-time manual login into the local profile")
    login.add_argument("--profile-dir", type=Path,
                       default=Path.home() / ".linkedin-connect-bot" / "profile")
    login.set_defaults(func=cmd_login)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
