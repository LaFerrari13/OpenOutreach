# openoutreach/linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up for one eligible CONNECTED deal."""
from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from openoutreach.crm.models import DealState
from openoutreach.linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3
MAX_UNANSWERED_OUTGOING = 1


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks."""
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _too_soon_to_nudge(deal) -> bool:
    """Wait ``unanswered_count * MIN_DAYS_PER_UNANSWERED`` days between nudges."""
    from openoutreach.chat.models import ChatMessage

    messages = ChatMessage.objects.filter(deal=deal)

    last = messages.order_by("-creation_date").first()
    if last is None or not last.is_outgoing:
        return False

    last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
    nudges = messages.filter(is_outgoing=True)
    if last_reply:
        nudges = nudges.filter(creation_date__gt=last_reply.creation_date)

    required = timedelta(days=nudges.count() * MIN_DAYS_PER_UNANSWERED)
    return timezone.now() - last.creation_date < required


def _last_messages_are_outgoing(deal, count: int = MAX_UNANSWERED_OUTGOING) -> bool:
    """Return True when the latest ``count`` messages are all from us."""
    from openoutreach.chat.models import ChatMessage

    latest = list(
        ChatMessage.objects.filter(deal=deal)
        .order_by("-creation_date", "-pk")[:count]
    )
    return len(latest) == count and all(msg.is_outgoing for msg in latest)


def _blocked_by_unanswered_outgoing_cap(deal, session, public_id: str) -> bool:
    """Hard-stop a conversation once two outgoing messages are unanswered."""
    if not _last_messages_are_outgoing(deal):
        return False

    from openoutreach.linkedin.db.chat import sync_conversation

    try:
        sync_conversation(session, public_id)
    except Exception:
        logger.exception("pre-follow-up sync failed for %s; preserving two-message guard", public_id)
        return True

    return _last_messages_are_outgoing(deal)


def _skip_unanswered_outgoing_cap(campaign, deal, public_id: str) -> None:
    logger.info(
        "[%s] follow_up: %s has %d unanswered outgoing messages — skipping",
        campaign, public_id, MAX_UNANSWERED_OUTGOING,
    )
    # Move this still-open conversation behind other CONNECTED deals.
    deal.save()


def _connected_deals(campaign):
    """Open, non-disqualified CONNECTED deals in *campaign*, oldest first."""
    from openoutreach.crm.models import Deal

    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.CONNECTED,
            outcome="",
            lead__disqualified=False,
        )
        .select_related("lead", "campaign")
        .order_by("update_date")
    )


def _next_followup_deal(campaign):
    """Oldest CONNECTED deal in *campaign* not on a nudge cooldown."""
    for deal in _connected_deals(campaign):
        if not _too_soon_to_nudge(deal):
            return deal
    return None


def handle_follow_up(task, session, qualifiers):
    from linkedin_cli.actions.message import send_raw_message
    from openoutreach.core.agents.follow_up import run_follow_up_agent
    from openoutreach.core.db.deals import capture_and_contribute, set_profile_state
    from openoutreach.core.db.summaries import materialize_profile_summary_if_missing

    campaign = session.campaign

    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        logger.info("[%s] follow_up: daily limit reached — slot skipped", campaign)
        return

    deal = _next_followup_deal(campaign)
    if deal is None:
        connected = _connected_deals(campaign).count()
        if connected:
            logger.info(
                "[%s] follow_up: %d connected lead(s), all within nudge cooldown — nothing due",
                campaign, connected,
            )
        else:
            logger.info("[%s] follow_up: no connected leads yet — nobody to follow up", campaign)
        return

    public_id = deal.lead.public_identifier
    logger.info(
        "[%s] %s %s",
        campaign, colored("▶ follow_up", "green", attrs=["bold"]), public_id,
    )

    # Retry the contact-info overlay while still email-empty — LinkedIn may not
    # have exposed the 1st-degree address at connect time; a later visit picks it
    # up and contributes it. No-op once an email is captured.
    capture_and_contribute(deal.lead, session)

    if _blocked_by_unanswered_outgoing_cap(deal, session, public_id):
        _skip_unanswered_outgoing_cap(campaign, deal, public_id)
        return

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    profile = _build_send_profile(deal)

    if decision.action == "send_message":
        if _last_messages_are_outgoing(deal):
            _skip_unanswered_outgoing_cap(campaign, deal, public_id)
            return
        logger.info("[%s] follow_up message for %s: %s", campaign, public_id, decision.message)
        sent = send_raw_message(session, profile, decision.message)
        if not sent:
            set_profile_state(session, public_id, DealState.QUALIFIED.value)
            logger.warning("follow_up for %s: send failed — moving to QUALIFIED for re-connection", public_id)
            return
        session.linkedin_profile.record_action(
            ActionLog.ActionType.FOLLOW_UP, session.campaign,
        )
        # Persist the outgoing message locally and bump update_date so the
        # next slot's eligibility query respects the cooldown and moves
        # this deal to the back of the queue.
        from openoutreach.linkedin.db.chat import sync_conversation
        try:
            sync_conversation(session, public_id)
        except Exception:
            logger.exception("post-send sync failed for %s (best-effort)", public_id)
        deal.save()

    elif decision.action == "mark_completed":
        set_profile_state(session, public_id, DealState.COMPLETED.value, outcome=decision.outcome)
        logger.info("[%s] follow_up completed for %s: outcome=%s", campaign, public_id, decision.outcome)

    elif decision.action == "wait":
        # Bump update_date so the eligibility query cycles to a different deal
        # next time; this deal returns to the front only after others are touched.
        deal.save()
