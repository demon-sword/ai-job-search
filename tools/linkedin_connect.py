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
  python3 tools/linkedin_connect.py run --candidates queue.json        # dry-run
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


_CHALLENGE_URL = re.compile(r"/checkpoint/challenge", re.I)
_AUTHWALL_URL = re.compile(r"authwall|/login|/uas/login", re.I)


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

    def close(self) -> None:
        raise NotImplementedError


class PlaywrightDriver(InviteDriver):
    """Live LinkedIn driver. Created only for --live runs; the import stays
    lazy so dry runs, planning, and tests never need Playwright installed."""

    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
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
            connect = page.get_by_role("button", name=re.compile(r"Invite .* to connect|Connect", re.I)).first
            try:
                if connect.count() == 0:
                    more = page.get_by_role("button", name=re.compile(r"^More", re.I)).first
                    if more.count() == 0:
                        raise UnexpectedUI("no Connect control and no More menu on profile")
                    more.click(timeout=10000)
                    connect = page.get_by_role("button", name=re.compile(r"Invite .* to connect|Connect", re.I)).first
                    if connect.count() == 0:
                        raise UnexpectedUI("no Connect entry even under More")
                connect.click(timeout=10000)
                page.wait_for_timeout(1500)
            except InviteStop:
                raise
            except Exception as exc:
                raise UnexpectedUI(f"Connect control did not behave: {exc}") from exc
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
                if send.count() == 0:
                    raise UnexpectedUI("invite dialog has no Send button")
                send.click(timeout=10000)
                page.wait_for_timeout(1500)
            except InviteStop:
                raise
            except Exception as exc:
                raise UnexpectedUI(f"Send button did not behave: {exc}") from exc
            stop = self._page_state(page)
            if stop is not None:
                raise stop
            return "sent"
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
    retry of the failed - or any later - candidate.
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
    try:
        for candidate in candidates:
            if limit > 0 and (summary["sent"] + summary["would_send"] + summary["skipped"]) >= limit:
                break
            try:
                key = canonical_key(str(candidate.get("profile_url") or ""))
            except ValueError as exc:
                summary["skipped"] += 1
                record = {"ts": timestamp, "decision": "skip", "reason": f"unkeyable: {exc}", "live": live}
                append_audit(audit_path, record)
                summary["decisions"].append(record)
                continue
            known = (ledger.get("persons", {}) or {}).get(key)
            if known and known.get("status") in TERMINAL_STATUSES:
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
            person = {"status": None, "last_ts": timestamp, "score": score, "reasons": reasons}
            if outcome == "sent":
                person["status"] = "invited"
                ledger["sends"].append(timestamp)
                sent_this_run += 1
                summary["sent"] += 1
            elif outcome == "already_pending":
                person["status"] = "invited"
            elif outcome == "already_connected":
                person["status"] = "connected"
            else:
                person["status"] = str(outcome)
            ledger.setdefault("persons", {})[key] = person
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
    candidates = load_candidates(args.candidates)
    ledger_path = args.state_dir / LEDGER_NAME
    audit_path = args.state_dir / AUDIT_NAME
    ledger = load_ledger(ledger_path)
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

    run = sub.add_parser("run", parents=[common], help="run the queue (dry-run unless --live)")
    run.add_argument("--candidates", type=Path, required=True)
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
