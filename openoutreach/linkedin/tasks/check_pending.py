# openoutreach/linkedin/tasks/check_pending.py
"""Check pending task — re-checks one due PENDING deal in the campaign.

Lazy: the slot carries only ``campaign_id``. The handler picks the
oldest-due PENDING deal at execution time. If the recheck leaves the
deal in PENDING, the backoff is doubled and ``next_check_pending_at``
re-stamped via the ``on_deal_state_entered`` hook.
"""
from __future__ import annotations

import logging

from django.utils import timezone
from termcolor import colored

from openoutreach.core.db.deals import set_profile_state
from openoutreach.crm.models import DealState
from linkedin_cli.exceptions import ProfileInaccessibleError, SkipProfile

logger = logging.getLogger(__name__)


def _next_due_pending_deal(campaign):
    from openoutreach.crm.models import Deal

    return (
        Deal.objects.filter(
            campaign=campaign,
            state=DealState.PENDING,
            next_check_pending_at__lte=timezone.now(),
        )
        .select_related("lead", "campaign")
        .order_by("next_check_pending_at")
        .first()
    )


def _double_backoff(deal) -> float:
    from openoutreach.core.conf import CAMPAIGN_CONFIG
    current = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
    deal.backoff_hours = current * 2
    deal.save(update_fields=["backoff_hours"])
    return deal.backoff_hours


def handle_check_pending(task, session, qualifiers):
    from linkedin_cli.actions.status import get_connection_status

    campaign = session.campaign
    deal = _next_due_pending_deal(campaign)
    if deal is None:
        logger.info("[%s] check_pending: no due PENDING deals — slot skipped", campaign)
        return

    public_id = deal.lead.public_identifier
    logger.info(
        "[%s] %s %s",
        campaign, colored("▶ check_pending", "magenta", attrs=["bold"]), public_id,
    )

    profile = deal.lead.to_profile_dict()
    profile_for_status = profile.get("profile") or profile

    try:
        # The library returns the observed UI state as a str; lift it into our enum.
        new_state = DealState(get_connection_status(session, profile_for_status).value)
    except ProfileInaccessibleError as e:
        logger.warning("Profile inaccessible — marking FAILED: %s", e)
        set_profile_state(
            session,
            public_id,
            DealState.FAILED.value,
            reason=f"Profile inaccessible: {e}",
        )
        return
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        set_profile_state(session, public_id, DealState.FAILED.value)
        return

    if new_state == DealState.PENDING:
        # Still pending — double the backoff before set_profile_state so the
        # state hook re-stamps next_check_pending_at with the doubled value.
        old = deal.backoff_hours or 0
        new = _double_backoff(deal)
        logger.info("%s still pending — backoff %.1fh → %.1fh", public_id, old, new)

    set_profile_state(session, public_id, new_state.value)
