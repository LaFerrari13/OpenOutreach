# openoutreach/emails/nudge.py
"""Per-launch email-setup nudge.

Runs every `rundaemon` start after onboarding. Email outreach has two independent
upgrades — BetterContact finding and IceMail sending — and the shared contacts
cache resolves emails for free, so neither depends on the other: a mailbox is
worth adding without the finder, and a key is worth setting before any mailbox
exists. Each launch the nudge offers whichever isn't configured yet, on its own —
prompting on a TTY, logging headless. Copy is the GLF angle from
marketing/email-sequence.md, filled with the user's own pipeline numbers. Never
blocks: email is a deferrable upgrade.
"""
from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from openoutreach.core.conf import DEFAULT_CONNECT_DAILY_LIMIT, DEFAULT_EMAIL_DAILY_LIMIT
from openoutreach.core.logging import brand
from openoutreach.core.models import SiteConfig
from openoutreach.core.onboarding_wizard import _BACK, IntText, MultilineText, Password
from openoutreach.crm.models import Deal, DealState, Lead
from openoutreach.emails import bettercontact
from openoutreach.emails.icemail import parse_mailboxes
from openoutreach.emails.models import Mailbox, has_mailbox
from openoutreach.emails.smtp import verify_auth
from openoutreach.linkedin.models import LinkedInProfile

logger = logging.getLogger(__name__)

BETTERCONTACT_AFFILIATE_URL = "https://bettercontact.rocks?fpr=openoutreach"
SENDER_AFFILIATE_URL = "https://icemail.ai?via=openoutreach"
EXPLAINER_URL = "https://openoutreach.app/email-outreach"


# ── Nudge copy ───────────────────────────────────────────────────

# GAIN — the discovery engine already worked; email is the reach you're missing.
BETTERCONTACT_NUDGE = """
LinkedIn finds the right people; email is how you reach them.

Your model qualified {qualified} leads, but LinkedIn sends only ~{connect_cap}/day and most never accept. Email reaches the whole list — automatically, as each one qualifies.

Turn on BetterContact email finding (paid; the affiliate fee keeps OpenOutreach free). Your first 50 lookups are free with the subscription, so you can try it at no cost:

    {bettercontact_url}

Finding an address and reaching it are separate steps — here's how email outreach fits together, and why it uses a separate sending domain:

    {explainer_url}
"""

# URGENCY — the ~2-week warmup clock (always true); a loss-aversion line only
# when the pipeline numbers are real (they're zero right after BetterContact is set).
MAILBOX_NUDGE = """
Set up email sending. {icemail} mailboxes need a ~2-week warmup, and the clock only starts once you add them — so the sooner they're warming, the sooner you reach the leads who never accept a LinkedIn connection.

{waiting_line}Add your sending mailboxes ({icemail} — paid; warmup is hands-off):

    {sender_url}
"""


def _hyperlink(url: str) -> str:
    """Wrap *url* in an OSC 8 terminal hyperlink so the whole address is clickable.

    Terminals' bare-URL detection often stops at the ``?``, leaving affiliate
    query params (``?fpr=...``) unclickable. OSC 8 marks the entire URL as one
    link explicitly. The visible text stays the URL itself, so terminals without
    OSC 8 support still show a copyable address.
    """
    esc = "\033"
    return f"{esc}]8;;{url}{esc}\\{url}{esc}]8;;{esc}\\"


def render(template: str, stats: dict, *, hyperlink: bool = False) -> str:
    """Fill a nudge *template* with the user's pipeline numbers.

    ``hyperlink=True`` wraps the affiliate URLs in OSC 8 escapes for an
    interactive TTY; leave it False for headless logging (no escape codes).
    """
    wrap = _hyperlink if hyperlink else (lambda u: u)
    return template.format(
        bettercontact_url=wrap(BETTERCONTACT_AFFILIATE_URL),
        sender_url=wrap(SENDER_AFFILIATE_URL),
        explainer_url=wrap(EXPLAINER_URL),
        waiting_line=_waiting_line(stats),
        icemail=brand("icemail"),
        **stats,
    )


def _waiting_line(stats: dict) -> str:
    """The mailbox nudge's loss-aversion line — shown only when its number is real.

    Right after BetterContact is enabled nothing has resolved yet, so both counts are
    zero and the line is omitted; the warmup urgency carries the message. Returns a
    full line ending in a newline, or '' to collapse it out of the copy.
    """
    if stats.get("resolved_emails"):
        return f"{stats['resolved_emails']} leads already have an email resolved, waiting to be reached.\n"
    if stats.get("pending"):
        return f"{stats['pending']} leads sit behind connection requests that may never be accepted.\n"
    return ""


def pipeline_stats() -> dict:
    """The user's own numbers — what makes the nudge land instead of nag."""
    profile = LinkedInProfile.objects.filter(active=True).first()
    return {
        "qualified": Deal.objects.filter(state=DealState.QUALIFIED).count(),
        "pending": Deal.objects.filter(state=DealState.PENDING).count(),
        "resolved_emails": Lead.objects.filter(api_email__isnull=False).count(),
        "connect_cap": profile.connect_daily_limit if profile else DEFAULT_CONNECT_DAILY_LIMIT,
    }


# ── Mailbox import (parse → auth-check → store; no console I/O) ───

@dataclass
class ImportReport:
    parsed: int = 0
    stored: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (email, reason)


def import_mailboxes(pasted: str, daily_limit: int = DEFAULT_EMAIL_DAILY_LIMIT) -> ImportReport:
    """Parse an App-Passwords paste, then auth-check and store each box.

    Raises ValueError (from ``parse_mailboxes``) when the paste isn't the App
    Passwords sheet; per-box auth failures are collected in the report, not raised.
    """
    return _store_mailboxes(parse_mailboxes(pasted), daily_limit)


def _store_mailboxes(rows: list[tuple[str, str]], daily_limit: int) -> ImportReport:
    """Auth-check each ``(email, app_password)`` and store only the ones that log in.

    A row exists iff it authenticated — there is no inactive state to carry.
    ``daily_limit`` is the warm-safe sends/day applied to each stored box.
    """
    report = ImportReport()
    for email, password in rows:
        report.parsed += 1
        box = Mailbox(username=email, password=password, from_address=email)
        ok, reason = verify_auth(box.host, box.port, box.username, box.password)
        if not ok:
            report.failures.append((email, reason))
            continue
        Mailbox.objects.update_or_create(
            username=email,
            defaults={"password": password, "from_address": email, "daily_limit": daily_limit},
        )
        report.stored += 1
    return report


# ── Per-launch prompt ────────────────────────────────────────────

def prompt_email_setup() -> None:
    """Offer each unconfigured email upgrade: BetterContact finding, IceMail sending.

    The two are independent (the shared contacts cache resolves emails for free), so
    they're two separate offers — skipping one still offers the other, and whatever
    stays unconfigured is re-offered next launch. On a TTY each offer prompts and
    collects; headless it only logs. Never `sys.exit`s, so it can't block the
    LinkedIn discovery leg.
    """
    if not bettercontact.is_configured():
        _offer(BETTERCONTACT_NUDGE, _collect_bettercontact_key)
    if not has_mailbox():
        _offer(MAILBOX_NUDGE, _collect_mailboxes)


def _offer(template: str, collect: Callable[[], None]) -> None:
    """Show one upgrade's nudge, then collect it on a TTY (or just log it headless).

    The collector handles its own failure modes (bad paste, auth reject) gracefully
    and leaves the upgrade unconfigured, so this never raises on user error.
    """
    if not sys.stdin.isatty():
        logger.info(render(template, pipeline_stats()))
        return
    print(render(template, pipeline_stats(), hyperlink=True))
    collect()


def _collect_bettercontact_key() -> None:
    key = Password("bettercontact_api_key", "BetterContact API key (Enter to skip):", required=False).ask("")
    if not key or key == _BACK:
        return
    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = key
    cfg.save()
    logger.info("BetterContact key saved — enrichment is on; emails resolve as leads qualify.")


def _collect_mailboxes() -> None:
    """Paste the App Passwords sheet, set the per-box cap, then auth-check + store."""
    rows = _ask_for_mailbox_rows()
    if rows is None:
        return  # user skipped
    _print_report(_store_mailboxes(rows, _ask_for_daily_limit()))


def _ask_for_mailbox_rows() -> list[tuple[str, str]] | None:
    """Prompt for the App Passwords paste, re-asking on an unrecognized sheet.

    Returns the parsed ``(email, app_password)`` rows, or None if the user skips.
    A wrong sheet (e.g. the login-credentials one) prints why and loops, so they
    can paste the right one without restarting.
    """
    while True:
        pasted = _ask_for_paste()
        if pasted is None:
            return None
        try:
            return parse_mailboxes(pasted)
        except ValueError as exc:
            print(f"  {exc}\n")


def _ask_for_daily_limit() -> int:
    """Per-mailbox warm-safe sends/day; Enter accepts the conservative default."""
    answer = IntText(
        "email_daily_limit",
        "Emails per mailbox per day (Enter for default):",
        default=DEFAULT_EMAIL_DAILY_LIMIT,
        required=False,
    ).ask(DEFAULT_EMAIL_DAILY_LIMIT)
    if not isinstance(answer, int) or answer <= 0:
        return DEFAULT_EMAIL_DAILY_LIMIT
    return answer


_PASTE_GUIDANCE = """\
  Open the App Passwords tab in the {icemail} XLS you downloaded (columns: Email,
  App Password) — NOT the login-credentials tab. Copy its rows with the header,
  paste below, then Ctrl+D to submit. (Enter = newline; No to skip.)
"""


def _ask_for_paste() -> str | None:
    """Prompt for the pasted App Passwords sheet; None if the user skips."""
    print(_PASTE_GUIDANCE.format(icemail=brand("icemail")))
    answer = MultilineText(
        "mailboxes",
        f"Paste your {brand('icemail')} App Passwords sheet",
        required=False,
    ).ask("")
    return None if not answer or answer == _BACK else answer


def _print_report(report: ImportReport) -> None:
    for email, reason in report.failures:
        print(f"  ✗ {email}: {reason}")
    if not report.parsed:
        print("  No mailboxes found — include the header row (Email, App Password).")
        return
    print(f"  Parsed {report.parsed} mailbox(es); {report.stored} authenticated and saved.")
