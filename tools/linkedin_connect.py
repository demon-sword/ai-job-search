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

Safety rails (all enforced in code, all covered by tests/test_linkedin_connect.py
and tests/test_linkedin_control.py):
1. Hard daily cap of 20 invites, counted from a durable local ledger
   (`~/.linkedin-connect-bot/state/ledger.json`, out of the repo so every
   worktree shares one ledger) that survives restarts. `--max-per-day`
   can only lower the cap, never raise it.
2. Dry-run default. Without `--live` the bot prints exactly what it WOULD
   send and sends nothing: no browser starts, the ledger is untouched.
3. Instant full stop - no retry, loud report, exit code 2 - on any login
   challenge, captcha, verification prompt, rate-limit response, or
   unexpected LinkedIn UI state.
4. Human-like pacing: a jittered sleep between invites, and sending is
   refused outright outside the configured daily window.
5. Append-only audit log (`~/.linkedin-connect-bot/state/audit.jsonl`):
   who, why they matched, timestamp, result. Never rewritten, only
   appended. One batch at a time holds the state-dir lock; automated
   loads refuse while the login window is open or the profile was
   touched outside the tool.
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

import hashlib
import shutil
import uuid
import argparse
import json
import os
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = ROOT / "tools" / "linkedin_connect_targets.json"
# Dispatch lives outside the repo on purpose: disposable worktrees must never
# reset caps or split the ledger, so the default state dir is a stable
# absolute path under the operator's home. --state-dir still overrides it.
DEFAULT_STATE_DIR = Path.home() / ".linkedin-connect-bot" / "state"
# Pre-lever state lived in the repo; resolve_state_dir() refuses a quiet
# default while an unmigrated ledger sits here, so caps can never silently
# restart from zero.
LEGACY_STATE_DIR = ROOT / "linkedin_connect"
DEFAULT_PROFILE_DIR = Path.home() / ".linkedin-connect-bot" / "profile"
LEDGER_NAME = "ledger.json"
AUDIT_NAME = "audit.jsonl"
# Single-writer discipline: one batch at a time holds this lock file in the
# state dir. Chromium's own profile lock is the backstop, this is the guard.
LOCK_NAME = "batch.lock"
# One-window login ceremony: `login` writes this marker while its browser
# window is open; automated loads refuse until it is gone.
LOGIN_MARKER_NAME = "login.open"
# Login-stability baseline: cookie mtime noted after every guarded load. A
# mismatch on the next load means something outside this tool touched the
# live profile, and the load is refused instead of burning the session.
PROFILE_SENTINEL_NAME = "profile_sentinel.json"
# Session liveness is a cheap heuristic, never a verdict: cookies touched
# within this window read "live", older reads "stale".
SESSION_STALE_DAYS = 7

HARD_DAILY_CAP = 20
HARD_WEEKLY_CAP = 90
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


class UncertainSend(InviteStop):
    signal = "uncertain"
    loud = "Invitation outcome is uncertain; reconcile before any further batch"


def normalize_button_name(raw: str) -> str:
    """Normalize one control name for comparison. Visible names carry icon
    glyphs ("+ Follow") that exact matching would otherwise miss; the strip
    is leading-symbols-only, so "Invite X to connect" never collapses onto
    "Connect" and a loose match can never click a wrong control again."""
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    return re.sub(r"^[^A-Za-z0-9]+", "", text)


def verify_dialog_target(dialog_text: str, candidate_name: str) -> bool:
    """True when an opened invite dialog addresses the candidate by full
    name. Every programmatic Connect click must pass this before Send:
    it is what makes page-wide control matching safe against inviting a
    stranger from a suggested-profile card."""
    want = re.sub(r"\s+", " ", str(candidate_name or "")).strip().lower()
    if not want:
        return False
    have = re.sub(r"\s+", " ", str(dialog_text or "")).strip().lower()
    return want in have


def is_exact_more_name(raw: str) -> bool:
    """True only for a control whose whole accessible name is More.
    Titled variants ("More options") and glyph-prefixed post menus
    ("… more") are NOT More: resolving expansion by this predicate keeps
    the click in the same query space as the match, so a Following button
    adjacent to More can never be resolved as the expansion target.
    Diagnosed live 2026-11-04: an index from the scan's button-only list
    was clicked as a button-role ordinal, landing on Following and opening
    an Unfollow confirmation."""
    return re.sub(r"\s+", " ", str(raw or "")).strip().lower() == "more"


# A 1st-degree profile carries no Connect control, so the More path can
# only burn misses against it. The degree badge ("Name · 1st") is the
# ground truth: Message alone is NOT enough (a 2nd-degree follow-state
# page showed Message yet still invited, 2026-11-04), and prose like
# "21st" never matches the middot badge form.
_FIRST_DEGREE_RE = re.compile(r"·\s*1st\b")


def is_first_degree_text(text: str) -> bool:
    """True when page text shows the 1st-degree badge form."""
    have = re.sub(r"\s+", " ", str(text or "")).strip()
    if not have:
        return False
    return _FIRST_DEGREE_RE.search(have) is not None


# Real Nov-21 captures have nested identity/contact and sibling actions inside
# one profile section. Bind that structural section first; never grow a root
# until a Connect appears. Its sole More + Message group bounds all controls.
TARGET_PROFILE_JS = r"""(candidate) => {
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();
    const visible = e => !!(e && e.getClientRects().length);
    const name = e => {
        const by = e.getAttribute('aria-labelledby');
        const labelled = by && by.split(/\s+/).map(id =>
            document.getElementById(id)?.innerText || '').join(' ');
        return norm(labelled || e.getAttribute('aria-label') || e.innerText);
    };
    const path = e => {
        const parts = [];
        for (let n = e; n && n.parentElement; n = n.parentElement)
            parts.unshift(n.tagName.toLowerCase() + ':nth-child(' +
                ([...n.parentElement.children].indexOf(n) + 1) + ')');
        return 'html > ' + parts.join(' > ');
    };
    const sanitizedHTML = card => {
        const clone = card.cloneNode(true);
        const originals = [card, ...card.querySelectorAll('*')];
        const copies = [clone, ...clone.querySelectorAll('*')];
        const allow = new Set(['role','aria-label','aria-labelledby','aria-expanded',
            'aria-disabled','aria-controls','id','href','title','disabled']);
        copies.forEach((e, i) => {
            for (const a of [...e.attributes]) if (!allow.has(a.name)) e.removeAttribute(a.name);
            e.setAttribute('data-readonly-visible', visible(originals[i]) ? 'true' : 'false');
            if (e.hasAttribute('href')) {
                try { const u = new URL(e.getAttribute('href'), location.href);
                    e.setAttribute('href', u.origin + u.pathname);
                } catch { e.removeAttribute('href'); }
            }
        });
        clone.querySelectorAll('script,style,iframe,input,textarea,form,img,svg,video')
            .forEach(e => e.remove());
        return clone.outerHTML;
    };
    const excluded = e => {
        if (e.closest('aside,[role="complementary"],[role="dialog"],[role="menu"]')) return true;
        for (let n = e; n && n !== document.body; n = n.parentElement) {
            const position = getComputedStyle(n).position;
            if (position === 'fixed' || position === 'sticky') return true;
        }
        return false;
    };
    const headings = root => [...root.querySelectorAll('h1,h2,[role="heading"]')]
        .filter(e => visible(e) && !excluded(e));
    const controls = root => [...root.querySelectorAll('button,a,[role="button"]')]
        .filter(e => visible(e) && !excluded(e));
    const result = {profile_url: location.origin + location.pathname,
        ready_state: document.readyState, title: document.title,
        name: null, verified: false, reason: 'missing_main',
        root_count: 0, heading_count: 0, action_group_count: 0,
        card_control_count: 0, control_count: 0, more_count: 0, controls: []};
    const main = document.querySelector('main,[role="main"]');
    if (!main) return result;
    const matches = headings(main).filter(e => norm(e.innerText) === norm(candidate.name));
    result.heading_count = matches.length;
    result.reason = matches.length > 1 ? 'ambiguous_heading' : 'missing_heading';
    if (matches.length !== 1) {
        if (candidate.diagnostic) {
            const sections = [...new Set((matches.length ? matches : headings(main).slice(0, 1))
                .map(e => e.closest('section')).filter(e => e && e !== main))];
            result.html = sections.map(sanitizedHTML).join('\n');
        }
        return result;
    }
    const heading = matches[0];
    // The nearest semantic section is the boundary, even when not ready.
    // Never climb past it to consume Activity, recommendations or main.
    const card = heading.closest('section');
    result.heading_name = norm(heading.innerText);
    result.heading_accessible_name = name(heading);
    if (!card || card === main || !main.contains(card)) {
        result.reason = 'unsupported_card';
        return result;
    }
    result.root_count = 1;
    result.selector = path(card);
    const own = controls(card).filter(e => e.closest('section') === card);
    const more = own.filter(e => name(e) === 'More' &&
        (e.tagName === 'BUTTON' || e.getAttribute('role') === 'button'));
    result.card_control_count = own.length;
    result.more_count = more.length;
    result.reason = 'incomplete_actions';
    if (candidate.diagnostic) {
        result.global_control_count = [...document.querySelectorAll('button,a,[role="button"]')]
            .filter(visible).length;
        result.card_controls = own.map(e => ({name: name(e), text: norm(e.innerText)}));
        result.html = sanitizedHTML(card);
    }
    const contact = own.filter(e => norm(e.innerText) === 'Contact info' &&
        e.tagName === 'A' && new URL(e.href).pathname.replace(/\/$/, '') ===
            location.pathname.replace(/\/$/, '') + '/overlay/contact-info');
    if (headings(card).length !== 1 || contact.length !== 1) {
        result.reason = 'ambiguous_card_identity';
        return result;
    }
    if (more.length !== 1) return result;
    // Current action-row wrappers have no role/classes stable across builds.
    // The innermost More+Message group is independent of Connect availability.
    let group = more[0].parentElement;
    while (group && group !== card && !controls(group).some(e =>
            /^Message(?:\s|$)/i.test(name(e)))) group = group.parentElement;
    if (!group || group === card || headings(group).length ||
            group.querySelector('aside,[role="complementary"],section') ||
            controls(group).some(e => norm(e.innerText) === 'Contact info')) return result;
    const actions = controls(group);
    if (actions.some(e => e.closest('section') !== card)) return result;
    result.action_group_count = 1;
    result.action_selector = path(group);
    result.controls = actions.map(e => name(e));
    result.control_count = actions.length;
    const pendingLabel = norm('Pending, click to withdraw invitation sent to ' + candidate.name).toLowerCase();
    result.pending = result.controls.some(s => /^(Pending|Invitation pending)$/i.test(s) ||
        s.toLowerCase() === pendingLabel);
    // Only the identity/contact region can supply degree, never nested content.
    let identity = heading.parentElement;
    while (identity && identity !== card && !identity.contains(contact[0]))
        identity = identity.parentElement;
    result.first_degree = !!identity && /·\s*1st\b/.test(norm(identity.innerText));
    result.receipts = [...document.querySelectorAll('[role="alert"],[role="status"],.artdeco-toast-item')]
        .filter(visible).map(e => norm(e.innerText));
    result.name = norm(heading.innerText);
    result.verified = true;
    result.reason = 'ready';
    return result;
}"""


def terminal_outcome(state: dict | None, candidate: dict, *, after_action: bool) -> str | None:
    """A receipt belongs to this URL and exact target card, not a suggestion."""
    if not isinstance(state, dict):
        return None
    try:
        if canonical_key(state.get("profile_url", "")) != canonical_key(candidate["profile_url"]):
            return None
    except (KeyError, ValueError):
        return None
    norm = lambda s: re.sub(r"\s+", " ", str(s or "")).strip().casefold()
    if not norm(candidate.get("name")) or norm(state.get("name")) != norm(candidate.get("name")):
        return None
    if state.get("pending"):
        return "sent" if after_action else "already_pending"
    if not after_action and state.get("first_degree"):
        return "already_connected"
    if after_action:
        wanted = "invitation sent to " + norm(candidate["name"])
        if any(norm(text).rstrip(".") == wanted for text in state.get("receipts", [])):
            return "sent"
    return None


_DESTRUCTIVE_HINTS = ("unfollow", "remove connection", "report", "block", "withdraw")


def destructive_hint(text: str) -> str | None:
    """The destructive action named by an open dialog's text, or None.
    Matches Unfollow / Remove-connection / Report / Block / Withdraw
    confirmations; invite dialogs ("Send invitation to ...") never match,
    so the guard fails closed toward a cancelled skip, never toward a
    blocked send."""
    have = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not have:
        return None
    for hint in _DESTRUCTIVE_HINTS:
        if hint in have:
            return hint
    return None


BUTTON_NAMES_JS = (
    "Array.from(document.querySelectorAll('button')).map(e => {"
    " const by = e.getAttribute('aria-labelledby');"
    " if (by) { const t = by.split(' ').map(id => "
    "((document.getElementById(id) || {}).innerText || '')).join(' ').trim();"
    " if (t) return t; }"
    " return (e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || '');"
    "})"
)

# More-menu item read for the live More -> Connect path. LinkedIn renders
# the overflow items as plain <li> under [role="menu"] (no menuitem role,
# no inner button/anchor), so the selector must include the li clause:
# without it the read sees only surrounding dialog chrome and a real
# Connect is misreported as a follow-only profile. Diagnosed live on
# 2026-11-02: a probe read with the li clause listed "Connect" while the
# run path without it saw nothing usable.
MORE_MENU_ITEM_SELECTOR = (
    '[role="menu"] [role="menuitem"], [role="menu"] button, '
    '[role="menu"] a, [role="menu"] li, [role="dialog"] button'
)
MORE_MENU_CONTAINER_SELECTOR = '[role="menu"], .artdeco-dropdown__content'
# NOTE for future edits: every JS blob below is a single-evaluated
# expression. Inside triple-quoted Python, a bare '\n' becomes a real
# newline and breaks the JS string it sits in - always write '\\n'. The
# ShippedJs suite pins structure (verbatim fragments, paren balance) for
# each blob; node --check the blob after any edit.
OVERLAY_STATE_JS = (
    """() => {
        const vis = (e) => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const path = (el) => {
            const parts = [];
            while (el && el !== document.body) {
                let i = 1, sib = el;
                while ((sib = sib.previousElementSibling)) i++;
                parts.unshift(el.tagName.toLowerCase() + ':nth-of-type(' + i + ')');
                el = el.parentElement;
            }
            return parts.join(' > ');
        };
        const controls = (root) => {
            const out = [];
            for (const b of root.querySelectorAll('button, [role="button"], a')) {
                const r = b.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) continue;
                const t = (b.getAttribute('aria-label') || b.innerText || '').trim();
                if (t && out.length < 12) out.push([t, path(b)]);
            }
            return out;
        };
        const out = [];
        const seen = new Set();
        const push = (controls) => {
            const key = controls.map((c) => c[0]).join('\\n');
            if (controls.length && !seen.has(key)) { seen.add(key); out.push({controls}); }
        };
        for (const d of document.querySelectorAll('[role="dialog"], [role="alertdialog"]')) {
            if (vis(d)) push(controls(d));
        }
        const vw = window.innerWidth || 1, vh = window.innerHeight || 1;
        for (const el of document.querySelectorAll('body div')) {
            let r;
            try {
                if (getComputedStyle(el).position !== 'fixed') continue;
                r = el.getBoundingClientRect();
            } catch (e) { continue; }
            if (r.width * r.height < 0.7 * vw * vh) continue;
            if (el.querySelector('[role="dialog"], [role="alertdialog"]')) continue;
            push(controls(el));
        }
        return out;
    }"""
)
MENU_HTML_JS = (
    "Array.from(document.querySelectorAll("
    "'[role=\"menu\"], .artdeco-dropdown__content'))"
    ".map(m => m.outerHTML || '').join('\\n')"
)
MORE_MENU_READ_JS = (
    # Union of two strategies, deduplicated: per-element accessible names
    # under role scopes (catches the role="menu" + menuitem render), plus
    # raw text lines of the open dropdown containers themselves (catches a
    # portal-rendered artdeco-dropdown whose items are click-handler divs
    # with no button/link/menuitem role). The Set matters: one item seen by
    # both strategies must not read as two "Connect"s and fail the
    # uniqueness check closed. The selector fragments are single Python
    # strings embedded verbatim - never re-split across fragments, which is
    # how a stray quote once shipped a reader that threw on every page.
    "Array.from(new Set(Array.from(document.querySelectorAll('"
    + MORE_MENU_ITEM_SELECTOR
    + "')).filter(e => e.getClientRects().length)"
      ".map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())"
      ".filter(Boolean).concat(Array.from(document.querySelectorAll('"
    + MORE_MENU_CONTAINER_SELECTOR
    + "')).filter(e => e.getClientRects().length)"
      ".map(m => m.innerText || '').join('\\n').split('\\n')"
      ".map(s => s.trim()).filter(Boolean))))"
)


def _norm_low(raw: str) -> str:
    return normalize_button_name(raw).lower()


def scan_entry_controls(names) -> dict:
    """Partition page-wide button accessible names (document order) into
    Connect-candidate indices, a follow flag, and More indices. Matching is
    deliberately loose here - "Connect Arnab Banerjee", bare "Connect",
    "+ Follow" all qualify. Actual controls and terminal states are bound to
    the target card before any press. "Connections" and "Invite X to
    connect" never match: the first is not an action, the second is a menu
    line handled by pick_invite_entry."""
    connect, more = [], []
    follow = False
    # Classifier only: the returned indices budget attempts but are never
    # clicked (their button-only ordinals live in a different index space
    # than button-role queries). Expansion resolves exact-name Mores via
    # is_exact_more_name instead - see _expand_more.
    for index, raw in enumerate(names or []):
        name = _norm_low(raw)
        if name == "connect" or name.startswith("connect "):
            if len(connect) < 3:
                connect.append(index)
        elif name == "follow" or name.startswith("follow "):
            follow = True
        elif name == "more" or name.startswith("more "):
            # "More options" and friends count: opening one only reads its
            # menu, and a menu without the named entry is escaped untouched.
            if len(more) < 3:
                more.append(index)
    return {"connect": connect, "follow": follow, "more": more}


FOLLOW_ONLY = "follow_only"
NOTE_REQUIRED = "note_required"
UNKNOWN_CONTROLS = "unknown_controls"
# Step-verified misses: the menu never yielded a verified entry, or a
# clicked Connect never yielded a verified dialog, after the one bounded
# re-attempt each. run_queue treats both exactly like unknown_controls
# (no ledger write, storm streak, skip with the step reason) - they are
# distinct outcomes so the audit says which transition failed.
MENU_MISSED = "menu_missed"
DIALOG_MISSED = "dialog_missed"
OVERLAY_BLOCKED = "overlay_blocked"
# A destructive confirmation (Unfollow/Remove/Report/Block/Withdraw) opened
# by a press: Cancel/Close it at once, screenshot it, and skip the profile.
# Never a ledger write (the profile stays queued) and never a re-press near
# the danger - but it still feeds the storm streak, because a LinkedIn
# redesign behind repeated avoids is UI drift that must halt the run.
DESTRUCTIVE_AVOIDED = "destructive_dialog_avoided"
STEP_MISSES = (UNKNOWN_CONTROLS, MENU_MISSED, DIALOG_MISSED, OVERLAY_BLOCKED,
               DESTRUCTIVE_AVOIDED)
# Invitable-confidence tiers for queue ordering: profiles with an observed
# direct Connect go first, More-overflow profiles second, and profiles whose
# entry variant is unknown last. The signal is recorded from cached data
# (discovery views, prior run outcomes) - never from extra page visits.
ENTRY_DIRECT = "direct"
ENTRY_MORE = "more"
ENTRY_UNKNOWN = "unknown"
ENTRY_TIERS = {ENTRY_DIRECT: 0, ENTRY_MORE: 1, ENTRY_UNKNOWN: 2}


def entry_variant(record: dict | None) -> str:
    """Tier name for one cached person/candidate record. An explicitly
    recorded entry wins; otherwise a prior follow_only/note_required status
    is cached evidence of creator-mode (More-or-nothing). Everything else is
    unknown - including never-visited queue entries."""
    record = record or {}
    seen = record.get("entry")
    if seen in ENTRY_TIERS:
        return seen
    if record.get("status") in (FOLLOW_ONLY, NOTE_REQUIRED):
        return ENTRY_MORE
    return ENTRY_UNKNOWN


def tier_name(candidate: dict, known: dict | None) -> str:
    """Tier name for one run candidate: its own recorded entry first,
    falling back to the ledger person discovery cached for it."""
    name = entry_variant(candidate)
    return name if name != ENTRY_UNKNOWN else entry_variant(known)


def queue_tier(candidate: dict, ledger: dict) -> int:
    """Sort rank for one run candidate (see tier_name)."""
    try:
        key = canonical_key(str(candidate.get("profile_url") or ""))
    except ValueError:
        return ENTRY_TIERS[ENTRY_UNKNOWN]
    known = (ledger.get("persons", {}) or {}).get(key)
    return ENTRY_TIERS[tier_name(candidate, known)]
# run skips these without sending; discovery never re-queues them.
# unknown_controls is deliberately NOT sticky: an unreadable profile is
# retried next run, while the per-run storm guard watches for UI drift.
RUN_SKIP_STATUSES = TERMINAL_STATUSES | {FOLLOW_ONLY, NOTE_REQUIRED, "attempting", "uncertain"}
DISCOVERY_STATUSES = RUN_SKIP_STATUSES | {"queued"}
MAX_CONSECUTIVE_UNKNOWN = 3
# Startup login grace: when the run's own window opens logged-out, wait
# this long (polling this often) for a live session instead of exit 2, so
# the operator can sign in inside that same window. Mid-run walls keep
# instant exit 2 - the grace covers startup only.
LOGIN_GRACE_S = 600
LOGIN_POLL_S = 15


def await_live_session(driver, *, grace_s=LOGIN_GRACE_S, interval_s=LOGIN_POLL_S,
                       sleeper=None, monotonic=None) -> bool:
    """Poll driver.session_is_live() until True or the grace expires.
    True the moment the window goes live; False on expiry. Injectable
    sleeper/monotonic keep tests instant."""
    sleep = sleeper or time.sleep
    now = monotonic or time.monotonic
    end = now() + grace_s
    while True:
        if driver.session_is_live():
            return True
        if now() >= end:
            return False
        sleep(interval_s)

_INVITE_ENTRY_RE = re.compile(r"^Invite (.+) to connect$", re.IGNORECASE)


def pick_invite_entry(names, candidate_name: str, bare_ok: bool = False) -> str | None:
    """Choose the exact More-menu string to click, or None. A full
    "Invite <candidate name> to connect" match (case-insensitive) always
    wins. With bare_ok (menu context only: the open menu belongs to the
    More just clicked, and the dialog target is verified afterwards), a
    unique bare "Connect" is clickable too - diagnosed live as LinkedIn's
    actual overflow wording. The default stays strict: a bare "Connect"
    anywhere else could belong to a suggested profile, and clicking that
    invites a stranger. Returns the observed string verbatim for an
    exact-name or exact-text resolution inside that dropdown only."""
    want = (candidate_name or "").strip().lower()
    bare = None
    bare_count = 0
    for raw in names or []:
        text = str(raw or "").strip()
        match = _INVITE_ENTRY_RE.match(text)
        if match and match.group(1).strip().lower() == want and want:
            return text
        if bare_ok and normalize_button_name(text).lower() == "connect":
            bare_count += 1
            bare = text
    if bare_ok and bare_count == 1:
        return bare
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
    return {"persons": {}, "sends": [], "queued_log": [], "attempts": {}, "reconciliations": {}}


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
    data.setdefault("queued_log", [])
    data.setdefault("attempts", {})
    data.setdefault("reconciliations", {})
    if not isinstance(data["attempts"], dict) or not isinstance(data["reconciliations"], dict):
        sys.exit(f"{path}: expected attempts and reconciliations to be objects")
    if not isinstance(data["persons"], dict) or not isinstance(data["sends"], list):
        sys.exit(f"{path}: expected persons to be an object and sends to be a list")
    if not isinstance(data["queued_log"], list):
        sys.exit(f"{path}: expected queued_log to be a list")
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
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def sends_today(ledger: dict, day: str) -> int:
    return sum(1 for ts in ledger.get("sends", []) if str(ts)[:10] == day)


def sends_last_days(ledger: dict, day: str, days: int) -> int:
    """Sends in the rolling window ending on day (inclusive, ISO dates
    compare lexicographically). A 7-day window covers day-6..day."""
    if days < 1:
        raise ValueError("window must be at least 1 day")
    return sum(1 for ts in ledger.get("sends", []) if day[:10] >= str(ts)[:10] >= _shift_day(day, days - 1))


def _shift_day(day: str, back: int) -> str:
    """ISO day string shifted back by N days (day is YYYY-MM-DD)."""
    from datetime import date

    year, month, dom = (int(part) for part in str(day)[:10].split("-"))
    return (date(year, month, dom) - timedelta(days=back)).isoformat()


def sends_last_7d(ledger: dict, day: str) -> int:
    return sends_last_days(ledger, day, 7)


def unresolved_attempts(ledger: dict) -> dict:
    return {key: record for key, record in ledger.get("attempts", {}).items()
            if record.get("status") in ("attempting", "uncertain")}


def reserved_count(ledger: dict, day: str, days: int = 1) -> int:
    first = _shift_day(day, days - 1)
    return sum(first <= str(record["reserved_ts"])[:10] <= day
               for record in unresolved_attempts(ledger).values())


def quota_today(ledger: dict, day: str) -> int:
    return sends_today(ledger, day) + reserved_count(ledger, day)


def quota_last_7d(ledger: dict, day: str) -> int:
    return sends_last_7d(ledger, day) + reserved_count(ledger, day, 7)


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


def default_state_dir() -> Path:
    """Stable absolute state home, resolved live so tests can point HOME at
    scratch. Never under the repo: any worktree resolves to the same dir."""
    return Path.home() / ".linkedin-connect-bot" / "state"


def resolve_state_dir(explicit: Path | None = None) -> Path:
    """On-demand dispatch root. An explicit --state-dir always wins;
    otherwise the out-of-repo default keeps every worktree on one ledger.
    Refuses (rather than silently restarting caps from zero) while a legacy
    in-repo ledger exists that was never migrated."""
    if explicit is not None:
        return Path(explicit)
    if not (DEFAULT_STATE_DIR / LEDGER_NAME).is_file() and (
        LEGACY_STATE_DIR / LEDGER_NAME
    ).is_file():
        sys.exit(
            f"refused: invite history lives in the legacy in-repo dir "
            f"{LEGACY_STATE_DIR / LEDGER_NAME} - move it once, then re-run: "
            f"mkdir -p {DEFAULT_STATE_DIR} && "
            f"mv {LEGACY_STATE_DIR / LEDGER_NAME} {DEFAULT_STATE_DIR / LEDGER_NAME} "
            f"(and audit.jsonl / discovery_cache.json alongside)"
        )
    return DEFAULT_STATE_DIR


class BatchLocked(Exception):
    """Another batch holds the state-dir lock: one batch at a time."""


class ProfileLoadRefused(Exception):
    """An automated browser load was refused to protect the login session."""


def _read_lock_holder(lock_path: Path) -> dict | None:
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class BatchLock:
    """Hold on the state-dir lock; release it when the batch is done."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def release(self) -> None:
        self._path.unlink(missing_ok=True)


def acquire_batch_lock(state_dir: Path, command: str) -> BatchLock:
    """Take the single-writer lock, or raise BatchLocked. A lock whose pid is
    dead is stale (crashed batch) and is taken over with a stderr notice."""
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_NAME
    record = json.dumps({"pid": os.getpid(), "cmd": command, "started": utcnow_iso()})
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = _read_lock_holder(lock_path)
        if holder is not None and _pid_alive(holder.get("pid")):
            raise BatchLocked(
                f"another {holder.get('cmd') or 'batch'} batch holds the lock "
                f"(pid {holder.get('pid')}, since {holder.get('started') or 'unknown'}) - "
                f"one batch at a time; wait for it to finish"
            )
        lock_path.write_text(record, encoding="utf-8")
        print(f"took over a stale batch lock ({holder})", file=sys.stderr)
        return BatchLock(lock_path)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(record)
    return BatchLock(lock_path)


def _cookies_file(profile_dir: Path) -> Path | None:
    for rel in ("Network/Cookies", "Cookies", "Default/Network/Cookies",
                "Default/Cookies"):
        candidate = profile_dir / rel
        if candidate.is_file():
            return candidate
    return None


def cookies_mtime(profile_dir: Path) -> float | None:
    found = _cookies_file(profile_dir)
    return found.stat().st_mtime if found is not None else None


def check_profile_load(state_dir: Path, profile_dir: Path) -> str | None:
    """Guard every automated browser load. Returns a refusal reason, or None
    when the load may proceed. Refuses while the one-window login ceremony
    is open, when the profile has no cookie store, and when the cookies
    changed outside this tool since the last guarded load (a second browser
    on the same profile burns the session - that is the overnight killer)."""
    marker = state_dir / LOGIN_MARKER_NAME
    if marker.exists():
        holder = _read_lock_holder(marker)
        who = f" (pid {holder.get('pid')})" if holder and holder.get("pid") else ""
        return (
            f"a login window is still open{who} - finish the one-window login "
            f"ceremony first; automated loads stay out until {LOGIN_MARKER_NAME} is gone"
        )
    sentinel_path = state_dir / PROFILE_SENTINEL_NAME
    if not sentinel_path.is_file():
        return None  # first guarded load bootstraps the baseline
    current = cookies_mtime(profile_dir)
    if current is None:
        return (
            "the live profile has no cookie store - run the login ceremony "
            "(tools/linkedin_connect.py login) before any batch"
        )
    try:
        noted = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # corrupt baseline heals itself on the next noted load
    if not isinstance(noted, dict) or noted.get("cookies_mtime") != current:
        return (
            "the profile cookies changed outside this tool since the last "
            "guarded load - another browser may be holding the live profile; "
            "re-run the login ceremony to re-verify before any batch"
        )
    return None


def note_guarded_load(state_dir: Path, profile_dir: Path, by: str) -> None:
    """Record the cookie baseline after a guarded load (or login)."""
    state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = {
        "cookies_mtime": cookies_mtime(profile_dir),
        "noted_ts": utcnow_iso(),
        "by": by,
    }
    (state_dir / PROFILE_SENTINEL_NAME).write_text(
        json.dumps(sentinel, indent=2), encoding="utf-8"
    )


class GuardedBrowserLoad:
    """One batch at a time, no loads during the login window, baseline noted.

    Raises BatchLocked when another batch holds the state-dir lock and
    ProfileLoadRefused when the login guard says no. The cookie baseline is
    noted on the way out, success or safety-stop alike - the browser loaded,
    so the baseline moved.
    """

    def __init__(self, state_dir: Path, profile_dir: Path, command: str) -> None:
        self._state_dir = state_dir
        self._profile_dir = profile_dir
        self._command = command
        self._lock: BatchLock | None = None

    def __enter__(self) -> "GuardedBrowserLoad":
        self._lock = acquire_batch_lock(self._state_dir, self._command)
        reason = check_profile_load(self._state_dir, self._profile_dir)
        if reason is not None:
            try:
                ledger = load_ledger(self._state_dir / LEDGER_NAME)
                ledger["last_error"] = dict(ts=utcnow_iso(), reason=f"profile_load_refused: {reason}")
                save_ledger(self._state_dir / LEDGER_NAME, ledger)
            finally:
                self._lock.release()
                self._lock = None
            raise ProfileLoadRefused(reason)
        return self

    def __exit__(self, *exc) -> bool:
        assert self._lock is not None
        try:
            note_guarded_load(self._state_dir, self._profile_dir, self._command)
        finally:
            self._lock.release()
            self._lock = None
        return False


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

    def session_is_live(self) -> bool:
        """True when the open window holds a live LinkedIn session
        (feed loads with no login wall). Used only by the startup login
        grace, before any send is attempted."""
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

    def __init__(self, profile_dir: Path, places=(), capture_dir=None):
        self.profile_dir = profile_dir
        self._places = [str(p).lower() for p in places or [] if str(p).strip()]
        self._capture_dir = Path(capture_dir) if capture_dir is not None else None
        self._pw = None
        self._ctx = None
        self.before_action = None

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

    def check_session(self) -> InviteStop | None:
        """One live probe: load the feed and classify it. Returns the stop
        signal when the session is challenged, else None. Read-only: no
        invite, no queue write."""
        page = self._ctx.new_page()
        try:
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2500)
            except Exception as exc:
                raise UnexpectedUI(f"probe page did not load: {exc}") from exc
            return self._page_state(page)
        finally:
            page.close()

    def session_is_live(self) -> bool:
        """Only a plain login wall may enter manual-login grace."""
        page = self._ctx.new_page()
        try:
            try:
                page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded",
                          timeout=20000)
                page.wait_for_timeout(2500)
            except Exception as exc:
                raise UnexpectedUI(f"startup feed did not load: {exc}") from exc
            stop = self._page_state(page)
            if isinstance(stop, LoginChallenge):
                return False
            if stop is not None:
                raise stop
            return True
        finally:
            try:
                page.close()
            except Exception:
                pass

    def send_invite(self, candidate: dict, note: str | None) -> str:
        page = self._ctx.new_page()
        outcome = None
        reason = UNKNOWN_CONTROLS
        try:
            try:
                page.goto(candidate["profile_url"], wait_until="domcontentloaded", timeout=20000)
            except Exception as exc:
                raise UnexpectedUI(f"profile page did not load: {exc}") from exc
            if self.before_action is None:
                raise ValueError("live invitations require a durable attempt reservation")
            state = self._await_target_card(page, candidate)
            if state is None:
                return UNKNOWN_CONTROLS
            outcome = terminal_outcome(state, candidate, after_action=False)
            if outcome is not None:
                return outcome
            slug = self._slug(candidate)
            obstruction = {"sightings": 0}
            if not self._dismiss_overlay(page, slug, obstruction):
                outcome = OVERLAY_BLOCKED
                return outcome
            outcome = self._direct_connect_flow(page, candidate, note, slug)
            if outcome is not None:
                return outcome
            # Only this target's More controls budget expansion. Neither
            # page-wide Follow nor an inaccessible More proves unavailability.
            scan = scan_entry_controls(state["controls"])
            outcome = self._more_connect_flow(page, scan["more"], candidate, note, obstruction)
            return outcome or UNKNOWN_CONTROLS
        except BaseException as exc:
            reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if outcome not in ("sent", "already_pending", "already_connected"):
                self._capture_target_failure(page, candidate, outcome or reason)
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

    def _escape(self, page) -> None:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        except Exception:
            pass

    def _overlay_state(self, page):
        """Visible obstructions, or None when the read fails. Each item is
        {"controls": [[name, css_path], ...]} for one dialog-role overlay or
        one full-viewport fixed scrim (ad lightboxes that carry no dialog
        role). The css path lets the clicker press the exact node with a
        trusted event instead of guessing page-wide."""
        try:
            return page.evaluate(OVERLAY_STATE_JS)
        except Exception:
            return None

    @staticmethod
    def _overlay_close_for(names: list) -> str | None:
        """A clean dismiss control among dialog button names: a Close/Dismiss
        (any suffix, e.g. "Close Modal Dialog") or a decline ("No thanks",
        "Not now", "Got it"). Never an accept - a dialog with no clean
        dismiss blocks instead of being agreed to."""
        for raw in names or []:
            first = str(raw or "").strip().lower().split(" ", 1)[0]
            if first in ("close", "dismiss"):
                return str(raw).strip()
            if str(raw or "").strip().lower() in ("no thanks", "not now", "got it"):
                return str(raw).strip()
        return None

    def _dismiss_overlay(self, page, slug: str, obs: dict | None = None) -> bool:
        """Dismiss a page-covering obstruction (dialog overlay or role-less
        ad lightbox) before any entry control is touched: it would otherwise
        swallow the clicks while reads see only its own buttons. Escape
        first, then one close/decline click on the exact node; screenshot
        before and after, and verify it is gone. `obs` tracks sightings on
        this page: an obstruction seen after two clears is reappearing, so
        block instead of clicking a third time. True when no obstruction
        remains. Never raises for mechanical failures (the downstream
        asserts still guard every click); only a confirmed persisting
        obstruction blocks. The challenge family still raises."""
        if obs is None:
            obs = {"sightings": 0}
        try:
            state = self._overlay_state(page) or []
        except Exception:
            return True
        if not state:
            return True
        obs["sightings"] = obs.get("sightings", 0) + 1
        if obs["sightings"] > 2:
            self._capture(page, slug, "overlay_reappeared", ok=False,
                          read={"sightings": obs["sightings"]})
            return False
        self._capture(page, slug, "overlay_before", ok=False,
                      read={"sightings": obs["sightings"]})
        self._escape(page)
        try:
            state = self._overlay_state(page) or []
        except Exception:
            return True
        if not state:
            self._capture(page, slug, "overlay_after", ok=True)
            return True
        path = None
        for item in state:
            controls = item.get("controls", []) if isinstance(item, dict) else []
            target = self._overlay_close_for([name for name, _ in controls])
            if target is not None:
                for name, css in controls:
                    if name == target:
                        path = css
                        break
                break
        if path is None:
            self._capture(page, slug, "overlay_after", ok=False,
                          read={"reason": "no clean dismiss control"})
            return False
        try:
            page.locator(path).first.click(timeout=10000)
            page.wait_for_timeout(1500)
        except InviteStop:
            raise
        except Exception:
            self._capture(page, slug, "overlay_after", ok=False,
                          read={"reason": "dismiss click failed"})
            return False
        stop = self._page_state(page)
        if stop is not None:
            raise stop
        try:
            state = self._overlay_state(page) or []
        except Exception:
            return True
        self._capture(page, slug, "overlay_after", ok=not state)
        return not state

    @staticmethod
    def _slug(candidate: dict) -> str:
        """Filesystem-safe profile slug for capture filenames."""
        try:
            key = canonical_key(str(candidate.get("profile_url") or ""))
        except ValueError:
            return "unkeyable"
        return re.sub(r"[^a-z0-9]+", "-", key.strip("/").lower()).strip("-") or "unkeyable"

    def _menu_html(self, page, limit: int = 20000) -> str:
        """Raw outerHTML of open menu containers (capped) - miss evidence
        showing exactly which nodes the read scanned."""
        try:
            html = page.evaluate(MENU_HTML_JS) or ""
        except Exception:
            return ""
        return str(html)[:limit]

    def _capture(self, page, slug: str, step: str, ok: bool, read=None) -> None:
        """Save a screenshot + DOM snippet for one verified step, win or
        lose. `read` optionally carries the step's raw read (menu names +
        menu HTML) so a miss preserves exactly what the matcher saw. Best
        effort only: a capture failure must never fail the run, and with no
        capture_dir configured nothing is written (tests)."""
        if self._capture_dir is None:
            return
        try:
            stamp = re.sub(r"[^0-9]", "", utcnow_iso()) or "00000000"
            base = self._capture_dir / f"{stamp}_{slug}_{step}_{'ok' if ok else 'miss'}"
            base.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(base.with_suffix(".png")))
            try:
                dom = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''")
            except Exception:
                dom = ""
            text = scrub(str(dom or ""))
            if read is not None:
                try:
                    text += "\n\nREAD " + json.dumps(read, ensure_ascii=False)[:20000]
                except Exception:
                    pass
            base.with_suffix(".txt").write_text(text, encoding="utf-8")
        except Exception:
            pass

    def _capture_target_failure(self, page, candidate: dict, reason: str) -> None:
        """Capture complete structural card evidence before a non-sending close.

        Screenshot/read failures are recorded independently; neither can hide the
        other. No credentials, URL query parameters or page-wide HTML are saved.
        """
        if self._capture_dir is None:
            return
        stamp = re.sub(r"[^0-9]", "", utcnow_iso())
        base = self._capture_dir / f"{stamp}_{self._slug(candidate)}_preaction"
        try:
            base.parent.mkdir(parents=True, exist_ok=True)
            url = urlparse(page.url)
            data = {"reason": reason, "final_url": url._replace(query="", fragment="").geturl()}
            try:
                data["target"] = page.evaluate(TARGET_PROFILE_JS, dict(candidate, diagnostic=True))
                html = data["target"].pop("html", "")
                if html:
                    base.with_suffix(".html").write_text(scrub(html), encoding="utf-8")
            except Exception as exc:
                data["read_error"] = scrub(str(exc))
            try:
                page.screenshot(path=str(base.with_suffix(".png")))
            except Exception as exc:
                data["screenshot_error"] = scrub(str(exc))
            base.with_suffix(".json").write_text(
                scrub(json.dumps(data, ensure_ascii=False, indent=2)), encoding="utf-8")
        except Exception:
            pass

    def _menu_entry_locator(self, page, entry_name: str):
        """Resolve the reader's exact accessible name OR visible text, uniquely.

        More must expose one visible dropdown. Never take the first of multiple
        entries/menus, or fall through a wrong-person accessible label to Connect
        text inside it. Hidden responsive copies cannot authorize a press.
        """
        menus = page.locator(MORE_MENU_CONTAINER_SELECTOR).filter(visible=True)
        if menus.count() != 1:
            return None
        if menus.evaluate("e => !!e.closest('aside,[role=\"complementary\"],[role=\"dialog\"]')"):
            return None
        entries = menus.get_by_label(entry_name, exact=True).or_(
            menus.get_by_text(entry_name, exact=True)).filter(visible=True)
        if entries.count() != 1:
            return None
        if not entries.evaluate(r"""(entry, wanted) => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
            const text = norm(entry.innerText).replace(/^[^a-z0-9]+/, '');
            if ((text !== 'connect' && text !== norm(wanted)) ||
                    entry.closest('[aria-disabled="true"],[disabled]')) return false;
            for (let e = entry; e && !e.matches('[role="menu"], .artdeco-dropdown__content');
                    e = e.parentElement) {
                const by = e.getAttribute('aria-labelledby');
                const label = (by && by.split(/\s+/).map(id =>
                    document.getElementById(id)?.innerText || '').join(' ')) ||
                    e.getAttribute('aria-label');
                if (label && norm(label) !== norm(wanted)) return false;
            }
            return true;
        }""", entry_name):
            return None
        return entries

    def _menu_entry_box(self, page, entry_name: str):
        """Viewport box of the dropdown entry, or None when it cannot be
        resolved to exactly one visible target."""
        try:
            target = self._menu_entry_locator(page, entry_name)
            return target.bounding_box(timeout=5000) if target is not None else None
        except Exception:
            return None

    def _human_press_entry(self, page, entry_name: str, candidate: dict, note: str | None,
                           slug: str, tag: str):
        """Press a verified menu entry the human way: re-verify the menu is
        still open with the entry, move the mouse to the item, hover-settle
        with a screenshot, re-verify again, then click at the item's coords.
        Trusted pointer events throughout - no JS event synthesis. Returns
        the send outcome, or None when any assert fails (caller escapes and
        moves on). The challenge family still raises."""
        name = candidate.get("name") or ""
        if pick_invite_entry(self._read_menu(page), name, bare_ok=True) != entry_name:
            self._capture(page, slug, f"{tag}_premove", ok=False)
            return None
        box = self._menu_entry_box(page, entry_name)
        if not box:
            self._capture(page, slug, f"{tag}_nobox", ok=False)
            return None
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        try:
            page.mouse.move(x, y)
            page.wait_for_timeout(800)
        except InviteStop:
            raise
        except Exception:
            self._capture(page, slug, f"{tag}_move", ok=False)
            return None
        self._capture(page, slug, f"{tag}_hover", ok=True)
        if pick_invite_entry(self._read_menu(page), name, bare_ok=True) != entry_name:
            self._capture(page, slug, f"{tag}_preclick", ok=False)
            return None
        return self._press_connect_and_send(
            page, lambda: page.mouse.click(x, y), candidate, note, slug, tag)

    def _keyboard_press_entry(self, page, pos: int, entry_name: str, candidate: dict,
                              note: str | None, slug: str, tag: str):
        """The one allowed fallback: re-open the More, keyboard-focus the
        entry, press Enter. Same asserts and captures as the pointer path."""
        # A failed pointer resolution may leave More expanded. Close before
        # reopening, rather than toggling that same menu shut as a "retry".
        self._escape(page)
        if not self._expand_more(page, pos, candidate):
            return None
        if pick_invite_entry(self._read_menu(page), candidate.get("name") or "",
                             bare_ok=True) != entry_name:
            self._capture(page, slug, f"{tag}_remenu", ok=False)
            self._escape(page)
            return None
        try:
            target = self._menu_entry_locator(page, entry_name)
            if target is None:
                return None
            target.focus(timeout=10000)
        except Exception:
            return None
        return self._press_connect_and_send(
            page, lambda: page.keyboard.press("Enter"), candidate, note, slug, tag)

    # Every Connect control is inside the verified target card: Connect can
    # itself send immediately, so post-click dialog identity is insufficient.
    DIRECT_CONNECT_RE = re.compile(r"^\s*Connect(\s|$)", re.I)

    def _profile_read(self, page, candidate: dict) -> dict | None:
        return page.evaluate(TARGET_PROFILE_JS, candidate)

    def _target_card(self, page, candidate: dict):
        """Verify the complete card and accessible identity; return its own actions."""
        state = self._profile_read(page, candidate)
        if (not isinstance(state, dict) or not state.get("verified")
                or not state.get("selector") or not state.get("action_selector")
                or state.get("name") != candidate.get("name")
                or canonical_key(state.get("profile_url", "")) != canonical_key(candidate["profile_url"])):
            raise UnexpectedUI("could not bind controls to the target profile card")
        card = page.locator(state["selector"]).filter(
            has=page.get_by_role("heading", name=candidate["name"], exact=True))
        actions = page.locator(state["action_selector"])
        if card.count() != 1 or actions.count() != 1:
            raise UnexpectedUI("target card accessible identity or action group is ambiguous")
        return actions

    def _await_target_card(self, page, candidate: dict, timeout: float = 10.0):
        """Bounded hydration of the verified card, with challenge checks each poll."""
        end = time.monotonic() + timeout
        while True:
            stop = self._page_state(page)
            if stop is not None:
                raise stop
            try:
                if canonical_key(page.url) != canonical_key(candidate["profile_url"]):
                    raise UnexpectedUI("profile navigation no longer identifies the target")
                state = self._profile_read(page, candidate)
                if state and state.get("reason") in ("ambiguous_heading", "ambiguous_card_identity"):
                    raise UnexpectedUI("target profile identity is ambiguous")
                if state and state.get("verified"):
                    self._target_card(page, candidate)
                    return state
            except InviteStop:
                raise
            except Exception:
                # Mechanical/hydration reads may settle; never classify them Follow.
                pass
            if time.monotonic() >= end:
                return None
            page.wait_for_timeout(250)

    def _direct_connect_controls(self, page, candidate: dict):
        return self._target_card(page, candidate).locator(
            'button:visible, a:visible, [role="button"]:visible').filter(has_text=self.DIRECT_CONNECT_RE)

    def _direct_connect_flow(self, page, candidate: dict, note: str | None, slug: str):
        """Only the target card's Connect controls are eligible for a press."""
        try:
            total = self._direct_connect_controls(page, candidate).count()
        except InviteStop:
            raise
        except Exception:
            return UNKNOWN_CONTROLS
        if total > 1:
            raise UnexpectedUI("multiple target Connect controls")
        for pos in range(total):
            outcome = self._press_connect_and_send(
                page,
                lambda k=pos: self._direct_connect_controls(page, candidate).nth(k).click(timeout=10000),
                candidate, note, slug, f"direct{pos}",
            )
            if outcome is not None:
                return outcome
            self._escape(page)
        return None

    def _press_connect_and_send(self, page, press, candidate: dict, note: str | None,
                                slug: str, tag: str):
        """A potentially sending press is never repeated on a missing dialog."""
        if getattr(self, "before_action", None) is not None:
            self.before_action()
        try:
            press()
        except InviteStop:
            raise
        except Exception as exc:
            self._capture(page, slug, f"{tag}_click", ok=False)
            raise UncertainSend(f"Connect may have reached LinkedIn: {exc}") from exc
        for poll in range(11):
            stop = self._page_state(page)
            if stop is not None:
                raise stop
            outcome = terminal_outcome(self._profile_read(page, candidate), candidate, after_action=True)
            if outcome is not None:
                self._capture(page, slug, f"{tag}_sent", ok=True)
                return outcome
            if self._dialog_targets(page, candidate):
                self._capture(page, slug, f"{tag}_dialog", ok=True)
                outcome = self._send_bare_from_dialog(page, note, candidate)
                if outcome == "sent":
                    self._capture(page, slug, f"{tag}_sent", ok=True)
                return outcome
            avoided = self._cancel_destructive_dialog(page, slug, tag)
            if avoided is not None:
                return avoided
            if poll < 10:
                page.wait_for_timeout(500)
        self._capture(page, slug, f"{tag}_uncertain", ok=False)
        raise UncertainSend("Connect produced neither a target receipt nor a verified invitation dialog")

    def _dialog_targets(self, page, candidate: dict) -> bool:
        """True when an open dialog addresses the candidate by name."""
        try:
            dialog = page.get_by_role("dialog").first
            if dialog.count() == 0:
                return False
            text = dialog.inner_text(timeout=5000)
        except Exception:
            return False
        return verify_dialog_target(text, candidate.get("name") or "")

    def _more_buttons(self, page, candidate: dict):
        return self._target_card(page, candidate).get_by_role("button", name="More", exact=True)

    def _expand_more(self, page, pos: int, candidate: dict) -> bool:
        """Open one More menu by exact accessible name. True when the click
        landed (the menu still needs its own read-assert); False on any
        mechanical failure. The challenge family raises."""
        try:
            self._more_buttons(page, candidate).nth(pos).click(timeout=10000)
            page.wait_for_timeout(1500)
        except InviteStop:
            raise
        except Exception:
            return False
        stop = self._page_state(page)
        if stop is not None:
            raise stop
        return True

    def _cancel_destructive_dialog(self, page, slug: str, tag: str) -> str | None:
        """Guard rail: the instant an Unfollow/Remove/Report/Block/Withdraw
        confirmation is open, press its Cancel (else Close) immediately,
        screenshot both states, and report DESTRUCTIVE_AVOIDED so the run
        skips the profile. A primary dialog button is NEVER pressed here -
        the only dialog that ever gets a primary press is the verified
        invite-Send dialog in _send_bare_from_dialog. Returns the outcome,
        or None when no destructive dialog is open. Mechanical failures
        fall back to Escape and still avoid; the challenge family raises."""
        try:
            dialog = page.get_by_role("dialog").first
            if dialog.count() == 0:
                return None
            text = dialog.inner_text(timeout=5000)
        except InviteStop:
            raise
        except Exception:
            return None
        hint = destructive_hint(text)
        if hint is None:
            return None
        self._capture(page, slug, f"{tag}_destructive", ok=False,
                      read={"hint": hint, "dialog": scrub(str(text or ""))[:500]})
        try:
            stop = self._page_state(page)
            if stop is not None:
                raise stop
            pressed = False
            for label in ("Cancel", "Close"):
                try:
                    btn = dialog.get_by_role("button", name=label, exact=True).first
                    if btn.count() > 0:
                        btn.click(timeout=10000)
                        page.wait_for_timeout(1500)
                        pressed = True
                        break
                except InviteStop:
                    raise
                except Exception:
                    continue
            if not pressed:
                self._escape(page)
        except InviteStop:
            raise
        except Exception:
            try:
                self._escape(page)
            except Exception:
                pass
        try:
            gone = page.get_by_role("dialog").first.count() == 0
        except Exception:
            gone = False
        self._capture(page, slug, f"{tag}_cancelled", ok=gone)
        return DESTRUCTIVE_AVOIDED

    def _read_menu(self, page) -> list:
        """Menu item names, or [] when the read itself fails. Callers assert
        non-empty + entry match before any click - an empty read is a miss,
        never a licence to click blind."""
        try:
            return page.evaluate(MORE_MENU_READ_JS) or []
        except Exception:
            return []

    def _profile_is_first_degree(self, page, candidate: dict) -> bool:
        return terminal_outcome(self._profile_read(page, candidate), candidate,
                                after_action=False) == "already_connected"

    def _more_connect_flow(self, page, more_indices: list, candidate: dict, note: str | None,
                             obstruction: dict | None = None):
        """Read bounded target-card More menus. A menu miss before any
        invitation action can skip; after Connect, only a positive receipt
        or a verified dialog permits progress. No speculative re-press."""
        slug = self._slug(candidate)
        name = candidate.get("name") or ""
        tried_any = False
        clicked_any = False
        if obstruction is None:
            obstruction = {"sightings": 0}
        # Attempt budget only: the scan's loose More indices are NOT clicked
        # (their button-role ordinals live in a different index space, which
        # mis-clicked Following on 2026-11-04). Each attempt expands the
        # pos-th exact-name More instead.
        for pos in range(min(len(more_indices), 3)):
            # Timed lightboxes pop mid-flow: re-verify the page is clear
            # before every expand, sharing the page's reappear counter.
            if not self._dismiss_overlay(page, slug, obstruction):
                return OVERLAY_BLOCKED
            if not self._expand_more(page, pos, candidate):
                continue
            tried_any = True
            avoided = self._cancel_destructive_dialog(page, slug, f"more{pos}")
            if avoided is not None:
                return avoided
            menu_names = self._read_menu(page)
            entry_name = pick_invite_entry(menu_names, name, bare_ok=True)
            if entry_name is None:
                # Assert failed (empty read or no usable entry): ONE bounded
                # re-expand + re-read of the same More before giving up on it.
                self._capture(page, slug, f"more{pos}_menu0", ok=False,
                              read={"menu_names": menu_names,
                                    "menu_html": self._menu_html(page)})
                self._escape(page)
                if self._expand_more(page, pos, candidate):
                    avoided = self._cancel_destructive_dialog(page, slug, f"more{pos}re")
                    if avoided is not None:
                        return avoided
                    menu_names = self._read_menu(page)
                    entry_name = pick_invite_entry(menu_names, name, bare_ok=True)
            if entry_name is None:
                self._capture(page, slug, f"more{pos}_menu1", ok=False,
                              read={"menu_names": menu_names,
                                    "menu_html": self._menu_html(page)})
                self._escape(page)
                continue
            self._capture(page, slug, f"more{pos}_menu", ok=True,
                          read={"menu_names": menu_names})
            clicked_any = True
            outcome = self._human_press_entry(
                page, entry_name, candidate, note, slug, f"more{pos}",
            )
            if outcome is not None:
                return outcome
            # The one allowed fallback: keyboard sequencing on the same More.
            outcome = self._keyboard_press_entry(
                page, pos, entry_name, candidate, note, slug, f"more{pos}kb",
            )
            if outcome is not None:
                return outcome
            self._escape(page)
        if clicked_any:
            return DIALOG_MISSED
        if tried_any:
            # Verified attempts, no usable entry: a 1st-degree page has no
            # Connect to find, so record it connected instead of burning
            # misses against it on every future run. run_queue already maps
            # "already_connected" to the connected status (never invited).
            if self._profile_is_first_degree(page, candidate):
                self._capture(page, slug, "first_degree", ok=True)
                return "already_connected"
            return MENU_MISSED
        return None

    def _send_bare_from_dialog(self, page, note: str | None, candidate: dict) -> str:
        """Click once inside the verified dialog; require a positive receipt."""
        stop = self._page_state(page)
        if stop is not None:
            raise stop
        if not self._dialog_targets(page, candidate):
            raise UncertainSend("invitation dialog no longer addresses the target")
        dialog = page.get_by_role("dialog").first
        if note:
            add_note = dialog.get_by_role("button", name=re.compile(r"Add a note", re.I)).first
            if add_note.count():
                add_note.click(timeout=10000)
                dialog.get_by_role("textbox").first.fill(note[:NOTE_LIMIT], timeout=10000)
        send = dialog.get_by_role("button", name=re.compile(r"^Send", re.I)).first
        if not send.count():
            if dialog.get_by_role("textbox").count():
                return NOTE_REQUIRED
            raise UnexpectedUI("verified invitation dialog has no Send button")
        if getattr(self, "before_action", None) is not None:
            self.before_action()
        try:
            send.click(timeout=10000)
        except Exception as exc:
            raise UncertainSend(f"Send may have reached LinkedIn: {exc}") from exc
        for poll in range(11):
            stop = self._page_state(page)
            if stop is not None:
                raise stop
            if terminal_outcome(self._profile_read(page, candidate), candidate, after_action=True) == "sent":
                return "sent"
            if poll < 10:
                page.wait_for_timeout(500)
        raise UncertainSend("Send has no positive target-specific acknowledgement")

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
            # Record the invite entry variant seen on this already-paid-for
            # view (direct Connect vs More-overflow vs unknown) so later
            # passes can order the queue from cache. Enrichment must never
            # fail the view itself: any read error keeps "unknown".
            try:
                names = driver_page.evaluate(BUTTON_NAMES_JS) or []
                scan = scan_entry_controls(names)
                if scan["connect"]:
                    entry = ENTRY_DIRECT
                elif scan["follow"] or scan["more"]:
                    entry = ENTRY_MORE
                else:
                    entry = ENTRY_UNKNOWN
            except Exception:
                entry = ENTRY_UNKNOWN
            return {
                "headline": data.get("headline") or "",
                "location": data.get("location") or "",
                "entry": entry,
            }
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
    """New profiles queued by discovery today - the discovery-side cap that
    mirrors the invite cap. Counted cumulatively from the append-only queue
    log: resolving a profile (send or skip) must NOT free its slot, or
    discovery could queue replacements without bound. Ledgers predating the
    log fall back to still-queued persons."""
    log = ledger.get("queued_log")
    if log:
        return sum(1 for ts in log if str(ts)[:10] == day)
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
    candidates: list[dict], cfg: dict, ledger: dict, audit_path: Path, *,
    live: bool, cap: int, day: str, window: str, note: str | None,
    limit: int, min_delay: float, max_delay: float, driver_factory=None,
    sleeper=None, rng: random.Random | None = None, now_iso: str | None = None,
    clock=None, login_grace_s=LOGIN_GRACE_S, login_interval_s=LOGIN_POLL_S,
) -> dict:
    """Ledger is authoritative; reserve before the driver, persist every result.

    Hard termination leaves attempting on disk. Catchable interruption records
    uncertain. Both require explicit reconciliation before another live pass.
    """
    rng = rng or random.Random()
    tick = clock or utcnow_iso
    stamp = lambda: now_iso or tick()
    timestamp = stamp()
    ledger_path = audit_path.with_name(LEDGER_NAME)
    summary = dict(live=live, day=day, cap=cap, sent=0, would_send=0, skipped=0,
                   already_pending=0, already_connected=0, uncertain=0, failed=0,
                   unknown_skips=0, unattempted=len(candidates), stopped=None, decisions=[])
    processed = 0
    run = None
    current_key = None
    driver = None

    def persist():
        if live:
            if run is not None:
                run["summary"] = {k: v for k, v in summary.items() if k != "decisions"}
                run["updated_at"] = timestamp
            save_ledger(ledger_path, ledger)

    def emit(record):
        summary["decisions"].append(record)
        summary["unattempted"] = len(candidates) - processed
        if run is not None:
            run["outcomes"].append(record)
        persist()
        append_audit(audit_path, record)

    reason = None
    if not inside_window(window):
        summary["stopped"], reason = "outside_window", f"outside daily window {window}"
    elif quota_today(ledger, day) >= cap:
        summary["stopped"], reason = "cap_reached", f"daily cap already reached ({cap})"
    elif quota_last_7d(ledger, day) >= HARD_WEEKLY_CAP:
        summary["stopped"], reason = "weekly_cap_reached", f"weekly cap already reached ({HARD_WEEKLY_CAP})"
    elif live and unresolved_attempts(ledger):
        summary["stopped"] = "reconciliation_required"
        reason = "reconcile unresolved attempts: " + ", ".join(unresolved_attempts(ledger))
    if reason:
        if live:
            ledger["last_error"] = dict(ts=timestamp, reason=reason)
        emit(dict(ts=timestamp, decision="refuse", reason=reason, live=live))
        return summary
    if not candidates:
        return summary
    if live:
        run = dict(id=uuid.uuid4().hex, pid=os.getpid(), status="running",
                   started_at=timestamp, updated_at=timestamp, outcomes=[])
        ledger["last_run"] = run
        summary["run_id"] = run["id"]
        persist()

    def checkpoint(*, action=False):
        nonlocal timestamp
        timestamp = stamp()
        ledger["attempts"][current_key].update(reserved_ts=timestamp, last_ts=timestamp)
        if action:
            attempt = ledger["attempts"][current_key]
            attempt["actions"] = attempt.get("actions", 0) + 1
        if not inside_window(window):
            raise UnexpectedUI("daily window closed before a sending action")
        if quota_today(ledger, timestamp[:10]) > cap or quota_last_7d(ledger, timestamp[:10]) > HARD_WEEKLY_CAP:
            raise UnexpectedUI("quota changed before a sending action")
        delay = pace_delay(min_delay, max_delay, rng)
        action_time = (datetime.fromisoformat(timestamp) if now_iso is not None or clock is not None
                       else datetime.now().astimezone())
        ledger["next_send_after"] = (action_time + timedelta(seconds=delay)).isoformat()
        persist()

    unknown_streak = 0
    try:
        if live:
            if driver_factory is None:
                raise ValueError("live run needs a driver factory")
            driver = driver_factory()
            driver.before_action = lambda: checkpoint(action=True)
            driver.start()
            if not driver.session_is_live():
                print("LOGIN-NEEDED - sign in manually in this run's window.", file=sys.stderr)
                if not await_live_session(driver, grace_s=login_grace_s,
                                          interval_s=login_interval_s, sleeper=sleeper):
                    summary["stopped"] = "login_needed"
                    timestamp = stamp()
                    emit(dict(ts=timestamp, decision="refuse", live=True,
                              reason=f"login grace expired ({login_grace_s}s) - no live session, nothing sent"))
                    return summary
        order = sorted(range(len(candidates)), key=lambda i: queue_tier(candidates[i], ledger))
        for idx in order:
            if limit > 0 and processed >= limit:
                break
            candidate = candidates[idx]
            timestamp = stamp()
            try:
                key = canonical_key(str(candidate.get("profile_url") or ""))
            except ValueError as exc:
                processed += 1
                unknown_streak = 0
                summary["skipped"] += 1
                emit(dict(ts=timestamp, decision="skip", reason=f"unkeyable: {exc}", live=live))
                continue
            known = ledger.get("persons", {}).get(key)
            score, reasons = score_candidate(candidate, cfg)
            base = dict(profile=key, name=candidate.get("name"), score=score, live=live)
            if known and known.get("status") in RUN_SKIP_STATUSES:
                processed += 1
                unknown_streak = 0
                summary["skipped"] += 1
                emit(dict(base, ts=timestamp, decision="skip", reason=f"dedupe: already {known['status']}"))
                continue
            if score < float(cfg.get("min_score", 0)):
                processed += 1
                unknown_streak = 0
                summary["skipped"] += 1
                emit(dict(base, ts=timestamp, decision="skip",
                          reason=f"below threshold ({score:g} < {float(cfg.get('min_score', 0)):g})"))
                continue
            if quota_today(ledger, timestamp[:10]) >= cap or quota_last_7d(ledger, timestamp[:10]) >= HARD_WEEKLY_CAP:
                summary["stopped"] = ("cap_reached" if quota_today(ledger, timestamp[:10]) >= cap
                                      else "weekly_cap_reached")
                emit(dict(base, ts=timestamp, decision="defer", reason=summary["stopped"]))
                break
            if not live:
                processed += 1
                summary["would_send"] += 1
                emit(dict(base, ts=timestamp, decision="dry-run", reason="; ".join(reasons),
                          tier=tier_name(candidate, known)))
                continue
            deadline = ledger.get("next_send_after")
            if deadline:
                remaining = (datetime.fromisoformat(deadline) - datetime.fromisoformat(timestamp)).total_seconds()
                if remaining > 0:
                    (sleeper or time.sleep)(remaining)
            timestamp = stamp()
            if not inside_window(window):
                summary["stopped"] = "outside_window"
                emit(dict(base, ts=timestamp, decision="defer", reason="window closed before next attempt"))
                break
            current_key = key
            processed += 1
            ledger.setdefault("attempts", {})[key] = dict(
                status="attempting", reserved_ts=timestamp, last_ts=timestamp,
                run_id=run["id"], candidate=dict(candidate))
            checkpoint()
            outcome = driver.send_invite(candidate, note)
            attempt = ledger["attempts"][key]
            if outcome == "sent" or attempt.get("actions", 0):
                # Start the next floor after completion, not before a slow click.
                checkpoint()
            elif deadline is None:
                ledger.pop("next_send_after", None)
            else:
                ledger["next_send_after"] = deadline
            timestamp = stamp()
            if outcome not in ("sent", "already_pending", "already_connected", FOLLOW_ONLY, NOTE_REQUIRED, *STEP_MISSES):
                raise UncertainSend(f"unrecognized driver outcome: {outcome}")
            attempt.update(status=outcome, last_ts=timestamp)
            if outcome in STEP_MISSES:
                unknown_streak += 1
                summary["skipped"] += 1
                summary["unknown_skips"] += 1
                emit(dict(base, ts=timestamp, decision="skip", result=outcome,
                          reason=f"{outcome}, {unknown_streak} consecutive"))
                current_key = None
                if unknown_streak > MAX_CONSECUTIVE_UNKNOWN:
                    summary["stopped"] = "unknown_controls"
                    break
                continue
            unknown_streak = 0
            person = dict(known or {})
            person.update({k: candidate.get(k) or person.get(k)
                           for k in ("profile_url", "name", "headline", "location", "company")})
            person.update(last_ts=timestamp, score=score, reasons=reasons)
            if outcome == "sent":
                person["status"] = "invited"
                ledger["sends"].append(timestamp)
                attempt["counted_ts"] = timestamp
                summary["sent"] += 1
            elif outcome in ("already_pending", "already_connected"):
                person["status"] = "invited" if outcome == "already_pending" else "connected"
                summary[outcome] += 1
            else:
                person["status"] = outcome
                summary["skipped"] += 1
            ledger.setdefault("persons", {})[key] = person
            emit(dict(base, ts=timestamp, decision="skip" if outcome in (FOLLOW_ONLY, NOTE_REQUIRED) else "send",
                      result=outcome, reason="; ".join(reasons), tier=tier_name(candidate, known)))
            current_key = None
    except BaseException as exc:
        timestamp = stamp()
        signal = exc.signal if isinstance(exc, InviteStop) else type(exc).__name__
        summary["stopped"] = signal
        if current_key and ledger["attempts"][current_key]["status"] == "attempting":
            attempt = ledger["attempts"][current_key]
            attempt.update(status="uncertain", last_ts=timestamp, reason=f"{signal}: {scrub(str(exc))}")
            summary["uncertain"] += 1
            ledger.setdefault("persons", {})[current_key] = dict(
                attempt["candidate"], status="uncertain", last_ts=timestamp)
        if run is not None:
            run["status"] = "stopped" if isinstance(exc, InviteStop) else "interrupted"
            run["reason"] = f"{signal}: {scrub(str(exc))}"
            ledger["last_error"] = dict(ts=timestamp, reason=run["reason"], profile=current_key)
        emit(dict(ts=timestamp, profile=current_key, decision="stop", reason=f"{signal}: {scrub(str(exc))}", live=live))
        raise
    finally:
        if run is not None:
            if run["status"] == "running":
                run["status"] = "stopped" if summary["stopped"] else "completed"
                run["reason"] = summary["stopped"]
                if summary["stopped"]:
                    ledger["last_error"] = dict(ts=timestamp, reason=summary["stopped"])
            run["finished_at"] = timestamp
            try:
                persist()
            finally:
                if driver is not None:
                    driver.close()
        elif driver is not None:
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
        if enriched.get("entry") in ENTRY_TIERS:
            candidate["entry"] = enriched["entry"]
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
    person = {
        "status": "queued",
        "last_ts": timestamp,
        "score": score,
        "reasons": reasons,
        "profile_url": candidate["profile_url"],
        "name": candidate["name"],
        "headline": candidate["headline"],
        "location": candidate["location"],
    }
    if candidate.get("entry") in ENTRY_TIERS:
        person["entry"] = candidate["entry"]
    ledger.setdefault("persons", {})[key] = person
    ledger.setdefault("queued_log", []).append(timestamp)
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
    state_dir = resolve_state_dir(args.state_dir)
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
    ledger_path = state_dir / LEDGER_NAME
    cache_path = state_dir / CACHE_NAME
    audit_path = state_dir / AUDIT_NAME
    ledger = cache = None
    day = today_str()

    def factory():
        return PlaywrightDriver(args.profile_dir, places=cfg.get("location_allow", []),
                                capture_dir=state_dir / "diag_run")

    def execute():
        nonlocal ledger, cache
        ledger = load_ledger(ledger_path)
        cache = load_cache(cache_path)
        if args.live and unresolved_attempts(ledger):
            raise UncertainSend("reconcile outstanding attempts before discovery")
        try:
            return run_discover(
                queries, cfg, ledger, cache, audit_path, live=args.live, day=day,
                window=args.window, max_searches=max_searches, max_pages=max_pages,
                max_profile_views=max_views, max_queued=max_queued,
                search_min_delay=search_min, search_max_delay=search_max,
                driver_factory=factory)
        finally:
            if args.live:
                save_ledger(ledger_path, ledger)
                save_cache(cache_path, cache)

    try:
        if args.live:
            with GuardedBrowserLoad(state_dir, args.profile_dir, "discover"):
                summary = execute()
        else:
            summary = execute()
    except (BatchLocked, ProfileLoadRefused) as exc:
        sys.exit(f"refused: {exc}")
    except InviteStop as stop:
        print(f"BOT STOPPED - {stop.loud}: {stop}", file=sys.stderr)
        print(f"Ledger and cache saved. No further searches attempted. Audit: {audit_path}", file=sys.stderr)
        return EXIT_STOPPED

    if args.live:
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
    state_dir = resolve_state_dir(args.state_dir)
    cfg = load_targets(args.targets)
    candidates = load_candidates(args.candidates)
    ledger = load_ledger(state_dir / LEDGER_NAME)
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
        if known and known.get("status") in RUN_SKIP_STATUSES:
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


def reconcile_records(state_dir: Path, manifest: dict, *, apply: bool = False) -> dict:
    """Explicit operator attestations, one atomic ledger update, immutable backup.

    Evidence files are hashed, not interpreted as automatic permission to retry.
    An identical id/content replay is a no-op; changed evidence refuses.
    """
    lock = acquire_batch_lock(state_dir, "reconcile") if apply else None
    try:
        ledger_path = state_dir / LEDGER_NAME
        if not ledger_path.is_file():
            raise ValueError("reconciliation requires the existing ledger; never initialize empty history")
        ledger = load_ledger(ledger_path)
        identifier = manifest.get("id", "")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifier):
            raise ValueError("manifest id must be a stable filesystem-safe identifier")
        if not isinstance(manifest.get("reason"), str) or not manifest["reason"].strip():
            raise ValueError("manifest requires an operator reason")
        if not isinstance(manifest.get("records"), list) or not manifest["records"]:
            raise ValueError("manifest requires explicit outcome records")
        records, seen = [], set()
        for row in manifest["records"]:
            candidate = row["candidate"]
            key = canonical_key(candidate["profile_url"])
            if key in seen:
                raise ValueError(f"duplicate reconciliation target: {key}")
            seen.add(key)
            outcome = row["outcome"]
            if outcome not in ("sent", "uncertain", "connected", "not_sent", UNKNOWN_CONTROLS):
                raise ValueError(f"unsupported reconciliation outcome: {outcome}")
            when = datetime.fromisoformat(row["ts"])
            if when.tzinfo is None:
                raise ValueError("evidence timestamps must include their timezone")
            evidence = []
            for filename in row.get("evidence", []):
                path = Path(filename).expanduser().resolve(strict=True)
                evidence.append(dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
            if not evidence:
                raise ValueError(f"explicit evidence is required for {key}")
            records.append(dict(profile=key, candidate=candidate, outcome=outcome,
                                ts=when.isoformat(), evidence=evidence))
        payload = dict(id=identifier, reason=manifest["reason"], records=records,
                       source_status=manifest.get("source_status", "reconciled"))
        if payload["source_status"] not in ("reconciled", "interrupted"):
            raise ValueError("source_status must be reconciled or interrupted")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        prior_reconciliation = ledger["reconciliations"].get(identifier)
        if prior_reconciliation:
            if prior_reconciliation["digest"] != digest:
                raise ValueError("reconciliation id already exists with different content/evidence")
            return dict(already_applied=True, id=identifier, backup=prior_reconciliation["backup"])
        for row in records:
            key, outcome, ts = row["profile"], row["outcome"], row["ts"]
            prior = ledger["attempts"].get(key, {})
            known = ledger["persons"].get(key, {})
            counted = prior.get("counted_ts") or (prior.get("last_ts") if prior.get("status") == "sent" else None)
            if counted and outcome in ("uncertain", "not_sent"):
                raise ValueError(f"cannot erase a confirmed send reservation/history: {key}")
            if outcome in ("sent", "uncertain") and known.get("status") == "invited" and not prior:
                raise ValueError(f"legacy invited target needs historical accounting review: {key}")
            if outcome == "not_sent" and prior.get("status") not in ("attempting", "uncertain", "not_sent"):
                raise ValueError(f"not_sent only resolves an unresolved attempt: {key}")
            if outcome == UNKNOWN_CONTROLS and (
                    known.get("status") != FOLLOW_ONLY or prior.get("status") != FOLLOW_ONLY
                    or prior.get("actions", 0) != 0 or counted
                    or known.get("name") != row["candidate"].get("name")):
                raise ValueError(f"unknown_controls only reclassifies a matching non-sending follow_only record: {key}")
            if outcome == "sent" and not counted:
                ledger["sends"].append(ts)
                counted = ts
            status = {"sent": "invited", "uncertain": "uncertain",
                      "connected": "connected", "not_sent": "retryable",
                      UNKNOWN_CONTROLS: "retryable"}[outcome]
            ledger["persons"][key] = {**known, **row["candidate"], "status": status, "last_ts": ts}
            ledger["attempts"][key] = dict(
                prior, status="already_connected" if outcome == "connected" else outcome,
                reserved_ts=ts, last_ts=ts, candidate=row["candidate"],
                reconciliation_id=identifier, evidence=row["evidence"], counted_ts=counted)
        day = today_str()
        result = dict(already_applied=False, applied=apply, id=identifier, records=records,
                      sent_today=sends_today(ledger, day), quota_today=quota_today(ledger, day),
                      quota_last_7d=quota_last_7d(ledger, day))
        if not apply:
            return result
        backup = state_dir / "backups" / (datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + identifier)
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        hashes = {}
        for name in (LEDGER_NAME, AUDIT_NAME, PROFILE_SENTINEL_NAME):
            source = state_dir / name
            if source.is_file():
                shutil.copy2(source, backup / name)
                hashes[name] = hashlib.sha256((backup / name).read_bytes()).hexdigest()
        backup_record = dict(payload, digest=digest, original_sha256=hashes)
        (backup / "manifest.json").write_text(json.dumps(backup_record, indent=2) + "\n", encoding="utf-8")
        now = utcnow_iso()
        ledger["reconciliations"][identifier] = dict(
            digest=digest, ts=now, reason=manifest["reason"], records=records, backup=str(backup))
        ledger["last_run"] = dict(id=identifier, status=payload["source_status"],
                                  reason=manifest["reason"], updated_at=now, outcomes=records)
        ledger["last_error"] = (dict(ts=now, reason="reconciliation required: " + ", ".join(unresolved_attempts(ledger)))
                                if unresolved_attempts(ledger) else None)
        latest = max(datetime.fromisoformat(row["ts"]) for row in records) + timedelta(seconds=DEFAULT_MIN_DELAY)
        if not ledger.get("next_send_after") or latest > datetime.fromisoformat(ledger["next_send_after"]):
            ledger["next_send_after"] = latest.isoformat()
        save_ledger(ledger_path, ledger)
        append_audit(state_dir / AUDIT_NAME, dict(ts=now, decision="reconcile",
                                                 id=identifier, records=records, backup=str(backup)))
        return dict(result, backup=str(backup))
    finally:
        if lock is not None:
            lock.release()


def cmd_reconcile(args) -> int:
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = reconcile_records(resolve_state_dir(args.state_dir), manifest, apply=args.apply)
    except (OSError, ValueError, KeyError, BatchLocked) as exc:
        print(f"reconciliation refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cmd_run(args) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    try:
        cap = effective_cap(args.max_per_day)
        parse_window(args.window)
    except ValueError as exc:
        sys.exit(str(exc))
    if args.min_delay < DEFAULT_MIN_DELAY or args.max_delay < args.min_delay:
        sys.exit("invite pacing must be at least 60 seconds")
    try:
        note = check_note(Path(args.note_file).read_text(encoding="utf-8") if args.note_file else None)
    except (OSError, ValueError) as exc:
        sys.exit(f"bad note file: {exc}")
    cfg = load_targets(args.targets)

    def execute():
        # Load only after acquiring the single-writer lock, not before waiting.
        ledger = load_ledger(state_dir / LEDGER_NAME)
        candidates = []
        if args.from_ledger:
            candidates = [dict(person) for person in ledger["persons"].values()
                          if person.get("status") == "queued" and person.get("profile_url")]
        if args.candidates is not None:
            candidates.extend(load_candidates(args.candidates))
        elif not args.from_ledger:
            sys.exit("pass --candidates queue.json or --from-ledger for discover-queued profiles")
        if not candidates:
            print("no queued profiles in the ledger - run discover first or pass --candidates.")
        return run_queue(
            candidates, cfg, ledger, state_dir / AUDIT_NAME, live=args.live, cap=cap,
            day=today_str(), window=args.window, note=note, limit=args.limit,
            min_delay=args.min_delay, max_delay=args.max_delay,
            driver_factory=lambda: PlaywrightDriver(args.profile_dir, capture_dir=state_dir / "diag_run"))

    try:
        if args.live:
            with GuardedBrowserLoad(state_dir, args.profile_dir, "run"):
                summary = execute()
        else:
            summary = execute()
    except (BatchLocked, ProfileLoadRefused) as exc:
        sys.exit(f"refused: {exc}")
    except (Exception, KeyboardInterrupt) as exc:
        print(f"BOT STOPPED - {getattr(exc, 'loud', type(exc).__name__)}: {scrub(str(exc))}",
              file=sys.stderr)
        run = load_ledger(state_dir / LEDGER_NAME).get("last_run", {})
        print(json.dumps(run, ensure_ascii=False))
        print("Durable reservations retained. Reconcile before retry; no further invites attempted.",
              file=sys.stderr)
        return 130 if isinstance(exc, KeyboardInterrupt) else EXIT_STOPPED
    if args.live:
        print(f"live: {summary['sent']} sent, {summary['skipped']} skipped, "
              f"{summary['already_pending']} already pending, {summary['already_connected']} already connected, "
              f"{summary['uncertain']} uncertain, {summary['failed']} failed, {summary['unattempted']} unattempted.")
    else:
        for row in summary["decisions"]:
            label = "WOULD SEND" if row["decision"] == "dry-run" else "SKIP"
            print(f"{label} {row.get('name') or row.get('profile')} [{row.get('profile')}]: {row['reason']}")
        print(f"dry-run: {summary['would_send']} would-send, {summary['skipped']} skipped - nothing sent, ledger untouched.")
    stop = summary["stopped"]
    if stop:
        detail = (f"{MAX_CONSECUTIVE_UNKNOWN + 1} consecutive unreadable profiles"
                  if stop == "unknown_controls" else stop)
        print(f"BOT STOPPED - {detail}", file=sys.stderr)
    if stop in ("unknown_controls", "login_needed", "reconciliation_required"):
        return EXIT_STOPPED
    return 1 if stop == "outside_window" else 0


def last_error_line(state_dir: Path) -> str | None:
    """Most recent stop/refusal audit row, flattened to one line. The counts
    are stdlib-only and never start a browser; so is this."""
    ledger = load_ledger(state_dir / LEDGER_NAME)
    pending = unresolved_attempts(ledger)
    if pending:
        return "reconciliation required: " + ", ".join(
            f"{key} ({record['status']})" for key, record in pending.items())
    if "last_error" in ledger:
        error = ledger["last_error"]
        return " ".join(str(v) for v in error.values() if v) if error else None
    audit_path = state_dir / AUDIT_NAME
    if not audit_path.is_file():
        return None
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("decision") in ("stop", "refuse"):
            flat = " ".join(
                str(row.get(key) or "") for key in ("ts", "decision", "reason")
            )
            return " ".join(flat.split())
    return None


def _human_age(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    if seconds < 120:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes}m"
    hours = int(minutes // 60)
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def session_state(profile_dir: Path) -> str:
    """Cheap liveness read: the cookie store's mtime, never a page load.

    A heuristic, not a verdict - cookies can be fresh on a dead session.
    Anything the tool cannot see from disk says unknown/stale, never live.
    """
    if not profile_dir.is_dir():
        return "unknown (no profile yet - run the login ceremony)"
    if _cookies_file(profile_dir) is None:
        return "dead (no cookie store in the profile - run the login ceremony)"
    age = time.time() - (cookies_mtime(profile_dir) or 0.0)
    touched = _human_age(age)
    if age > SESSION_STALE_DAYS * 86400:
        return f"stale (cookies untouched for {touched} - re-run login if invites fail)"
    if touched == "just now":
        return "recent cookie activity (heuristic only; authentication unverified)"
    return f"recent cookie activity ({touched} ago; authentication unverified)"


def cmd_status(args) -> int:
    state_dir = resolve_state_dir(args.state_dir)
    if args.probe:
        return cmd_probe(args, state_dir)
    ledger = load_ledger(state_dir / LEDGER_NAME)
    day = today_str()
    try:
        cap = effective_cap(args.max_per_day)
    except ValueError as exc:
        sys.exit(str(exc))
    by_status: dict[str, int] = {}
    for person in (ledger.get("persons") or {}).values():
        status = str(person.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    sent_day = sends_today(ledger, day)
    sent_week = sends_last_7d(ledger, day)
    used_day, used_week = quota_today(ledger, day), quota_last_7d(ledger, day)
    pending = unresolved_attempts(ledger)
    last_run = dict(ledger.get("last_run") or {})
    if last_run.get("status") == "running" and not _pid_alive(last_run.get("pid")):
        last_run["status"] = "interrupted"
    error = last_error_line(state_dir)
    session = session_state(args.profile_dir)
    if args.json:
        print(json.dumps({
            "day": day,
            "sent_today": sent_day,
            "cap": cap,
            "remaining_today": max(0, cap - used_day),
            "quota_today": used_day,
            "reserved_today": reserved_count(ledger, day),
            "sent_last_7d": sent_week,
            "weekly_cap": HARD_WEEKLY_CAP,
            "remaining_week": max(0, HARD_WEEKLY_CAP - used_week),
            "quota_last_7d": used_week,
            "reserved_last_7d": reserved_count(ledger, day, 7),
            "unresolved_attempts": pending,
            "last_run": last_run,
            "persons_by_status": by_status,
            "last_error": error,
            "session": session,
            "ledger": str(state_dir / LEDGER_NAME),
            "audit": str(state_dir / AUDIT_NAME),
        }, indent=2))
        return 0
    print(f"LinkedIn connect bot status ({day})")
    print(f"State: {state_dir}")
    print(f"Profile: {args.profile_dir}")
    print(f"Invites sent today: {sent_day} of {cap}; quota used {used_day} ({max(0, cap - used_day)} left)")
    print(f"Invites this week: {sent_week} of {HARD_WEEKLY_CAP}; quota used {used_week} "
          f"({max(0, HARD_WEEKLY_CAP - used_week)} left)")
    print(f"Reservations: {reserved_count(ledger, day)} today; {len(pending)} unresolved total")
    print(f"Last run: {last_run.get('id', 'none')} / {last_run.get('status', 'none')}")
    print(f"Last error: {error if error else 'none'}")
    print(f"Session: {session}")
    return 0


def cmd_probe(args, state_dir: Path) -> int:
    """One explicit live page load behind the same lock and guard as every
    other browser touch. Read-only: no invite, no queue write."""
    try:
        with GuardedBrowserLoad(state_dir, args.profile_dir, "probe"):
            driver = PlaywrightDriver(args.profile_dir,
                                      capture_dir=state_dir / "diag_run")
            try:
                driver.start()
                try:
                    stop = driver.check_session()
                finally:
                    driver.close()
            except InviteStop as stop:
                print(f"probe: {stop.signal}: {stop}", file=sys.stderr)
                return EXIT_STOPPED
    except (BatchLocked, ProfileLoadRefused) as exc:
        sys.exit(f"refused: {exc}")
    if stop is None:
        print("probe: signed in - feed loaded, no challenge shown")
        return 0
    print(f"probe: {stop.signal}: {stop}", file=sys.stderr)
    return EXIT_STOPPED


def cmd_login(args) -> int:
    """One-window ceremony: open a persistent browser for manual login.

    The operator signs in by hand inside the opened window (including any
    2FA), then closes it. Session cookies stay in the profile directory -
    this tool never sees, stores, or logs a password or token.

    One window at a time: this command refuses while another batch holds
    the state-dir lock, and writes the login-open marker so every automated
    load refuses until the ceremony is done. Full rite in
    tools/LINKEDIN_LOGIN.md.
    """
    state_dir = resolve_state_dir(args.state_dir)
    try:
        lock = acquire_batch_lock(state_dir, "login")
    except BatchLocked as exc:
        sys.exit(f"refused: {exc} - let that batch finish, then re-run login")
    marker = state_dir / LOGIN_MARKER_NAME
    marker.write_text(
        json.dumps({"pid": os.getpid(), "started": utcnow_iso()}), encoding="utf-8"
    )
    try:
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
    finally:
        marker.unlink(missing_ok=True)
        note_guarded_load(state_dir, args.profile_dir, "login")
        lock.release()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    common.add_argument("--state-dir", type=Path, default=None,
                          help="dispatch root; defaults out-of-repo to ~/.linkedin-connect-bot/state")

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
    discover.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
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
    run.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common],
                            help="plain-words quota, last error, and session liveness (no browser)")
    status.add_argument("--max-per-day", type=int, default=HARD_DAILY_CAP)
    status.add_argument("--json", action="store_true", help="machine-readable JSON instead of plain words")
    status.add_argument("--probe", action="store_true",
                        help="explicit flag: one live feed load to verify the session (starts a browser)")
    status.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    status.set_defaults(func=cmd_status)

    reconcile = sub.add_parser("reconcile", parents=[common],
                               help="preview/apply explicit evidence-backed outcomes; never starts a browser")
    reconcile.add_argument("--manifest", type=Path, required=True)
    reconcile.add_argument("--apply", action="store_true", help="back up state and commit these reconciliation records")
    reconcile.set_defaults(func=cmd_reconcile)

    login = sub.add_parser("login", parents=[common], help="one-window manual login into the local profile")
    login.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    login.set_defaults(func=cmd_login)
    return ap


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
