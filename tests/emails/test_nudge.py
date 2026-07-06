# tests/emails/test_nudge.py
"""The per-launch email nudge: state machine, copy, and mailbox import."""
from unittest.mock import MagicMock, patch

from openoutreach.core.models import Campaign, SiteConfig
from openoutreach.crm.models import DealState
from openoutreach.emails import nudge
from openoutreach.emails.models import Mailbox
from tests.factories import DealFactory, LeadFactory


def _set_bettercontact_key(value: str = "k"):
    cfg = SiteConfig.load()
    cfg.bettercontact_api_key = value
    cfg.save()


def _box(email="a@b.com"):
    return Mailbox.objects.create(username=email, password="p", from_address=email)


# ── Copy ─────────────────────────────────────────────────────────

def test_render_bettercontact_uses_numbers_and_link():
    out = nudge.render(nudge.BETTERCONTACT_NUDGE, {
        "qualified": 42, "pending": 0, "resolved_emails": 0, "connect_cap": 20,
    })
    assert "42" in out and "20" in out and nudge.BETTERCONTACT_AFFILIATE_URL in out
    assert nudge.EXPLAINER_URL in out  # points at the email-outreach explainer


def test_render_mailbox_always_shows_warmup_and_sender_link():
    out = nudge.render(nudge.MAILBOX_NUDGE, {
        "qualified": 0, "pending": 0, "resolved_emails": 0, "connect_cap": 20,
    })
    assert "warm" in out.lower() and nudge.SENDER_AFFILIATE_URL in out
    assert " 0 " not in out  # no awkward zero right after BetterContact is enabled


def test_render_mailbox_leads_with_resolved_count_when_present():
    out = nudge.render(nudge.MAILBOX_NUDGE, {
        "qualified": 0, "pending": 480, "resolved_emails": 312, "connect_cap": 20,
    })
    assert "312" in out  # resolved takes precedence over pending
    assert "480" not in out


def test_render_mailbox_falls_back_to_pending_when_nothing_resolved_yet():
    out = nudge.render(nudge.MAILBOX_NUDGE, {
        "qualified": 0, "pending": 480, "resolved_emails": 0, "connect_cap": 20,
    })
    assert "480" in out


def test_render_plain_has_no_escape_codes():
    out = nudge.render(nudge.BETTERCONTACT_NUDGE, {
        "qualified": 1, "pending": 0, "resolved_emails": 0, "connect_cap": 20,
    })
    assert "\033" not in out


def test_render_hyperlink_wraps_url_in_osc8():
    out = nudge.render(nudge.BETTERCONTACT_NUDGE, {
        "qualified": 1, "pending": 0, "resolved_emails": 0, "connect_cap": 20,
    }, hyperlink=True)
    # OSC 8 opener carries the URL, and the URL stays visible as the link text.
    assert f"\033]8;;{nudge.BETTERCONTACT_AFFILIATE_URL}\033\\" in out
    assert out.count(nudge.BETTERCONTACT_AFFILIATE_URL) == 2  # target + visible text


def test_pipeline_stats_counts_the_pipeline():
    campaign = Campaign.objects.create(name="stats-test")
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.QUALIFIED)
    DealFactory(campaign=campaign, lead=LeadFactory(), state=DealState.PENDING)
    DealFactory(campaign=campaign, lead=LeadFactory(api_email="x@y.com"), state=DealState.QUALIFIED)

    stats = nudge.pipeline_stats()
    assert stats["qualified"] == 2
    assert stats["pending"] == 1
    assert stats["resolved_emails"] == 1
    assert stats["connect_cap"] >= 1


# ── Per-launch offers (prompt_email_setup) ───────────────────────

def _tty(yes=True):
    return patch("openoutreach.emails.nudge.sys.stdin.isatty", return_value=yes)


def _stub_collectors():
    """Replace both collectors with mocks; returns (finder, mailbox)."""
    finder, mailbox = MagicMock(), MagicMock()
    return finder, mailbox, patch.multiple(
        "openoutreach.emails.nudge",
        _collect_bettercontact_key=finder,
        _collect_mailboxes=mailbox,
    )


def test_offers_both_upgrades_when_neither_is_configured():
    _set_bettercontact_key("")  # finder unconfigured; no mailbox either
    finder, mailbox, stub = _stub_collectors()
    with _tty(), patch("builtins.print"), stub:
        nudge.prompt_email_setup()
    finder.assert_called_once()
    mailbox.assert_called_once()


def test_skipping_the_finder_still_offers_the_mailbox():
    """The two are independent: a skipped finder must not block mailbox setup."""
    _set_bettercontact_key("")  # finder stays unconfigured (collector is a no-op)
    finder, mailbox, stub = _stub_collectors()
    with _tty(), patch("builtins.print"), stub:
        nudge.prompt_email_setup()
    finder.assert_called_once()
    mailbox.assert_called_once()  # offered despite the finder being skipped


def test_offers_only_the_mailbox_when_the_finder_is_already_set():
    _set_bettercontact_key("k")  # finder configured; still no mailbox
    finder, mailbox, stub = _stub_collectors()
    with _tty(), patch("builtins.print"), stub:
        nudge.prompt_email_setup()
    finder.assert_not_called()
    mailbox.assert_called_once()


def test_offers_nothing_when_both_are_configured():
    _set_bettercontact_key("k")
    _box()
    finder, mailbox, stub = _stub_collectors()
    with _tty(), patch("builtins.print"), stub:
        nudge.prompt_email_setup()
    finder.assert_not_called()
    mailbox.assert_not_called()


def test_headless_logs_pending_upgrades_without_collecting():
    _set_bettercontact_key("")  # both pending → both logged, neither collected
    finder, mailbox, stub = _stub_collectors()
    with _tty(False), stub, patch("openoutreach.emails.nudge.logger") as log:
        nudge.prompt_email_setup()
    finder.assert_not_called()
    mailbox.assert_not_called()
    assert log.info.call_count == 2


# ── Mailbox import ───────────────────────────────────────────────

_APP_PW_SHEET = "Email\tApp Password\na@b.com\twqig ioha mdvd pece"


def test_import_stores_box_when_auth_succeeds():
    with patch("openoutreach.emails.nudge.verify_auth", return_value=(True, "ok")):
        report = nudge.import_mailboxes(_APP_PW_SHEET)
    assert (report.parsed, report.stored, report.failures) == (1, 1, [])
    box = Mailbox.objects.get(username="a@b.com")
    assert box.from_address == "a@b.com"
    assert box.password == "wqigiohamdvdpece"  # spaces stripped from the app password


def test_import_skips_box_and_records_failure_on_auth_error():
    with patch("openoutreach.emails.nudge.verify_auth", return_value=(False, "auth rejected (534)")):
        report = nudge.import_mailboxes(_APP_PW_SHEET)
    assert report.stored == 0
    assert report.failures == [("a@b.com", "auth rejected (534)")]
    assert not Mailbox.objects.filter(username="a@b.com").exists()


def test_import_upserts_existing_mailbox_by_username():
    Mailbox.objects.create(username="a@b.com", password="old", from_address="a@b.com")
    with patch("openoutreach.emails.nudge.verify_auth", return_value=(True, "ok")):
        nudge.import_mailboxes(_APP_PW_SHEET)
    box = Mailbox.objects.get(username="a@b.com")
    assert box.password == "wqigiohamdvdpece"
    assert Mailbox.objects.filter(username="a@b.com").count() == 1
