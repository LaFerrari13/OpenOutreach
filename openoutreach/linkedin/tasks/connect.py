# openoutreach/linkedin/tasks/connect.py
"""Connect task — resolves one candidate from the campaign pool and acts.

Lazy: the task payload carries only ``campaign_id``. The handler picks
its candidate at execution time via the campaign's ``ConnectStrategy``.
No self-rescheduling — pacing is owned by ``tasks/scheduler.py``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from termcolor import colored

from openoutreach.core.db.deals import increment_connect_attempts, set_profile_state
from openoutreach.crm.models import DealState
from openoutreach.linkedin.db.leads import disqualify_lead
from openoutreach.linkedin.models import ActionLog
from linkedin_cli.exceptions import ProfileInaccessibleError, ReachedConnectionLimit, SkipProfile

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3


@dataclass
class ConnectStrategy:
    find_candidate: Callable
    pre_connect: Callable | None
    qualifier: object


def strategy_for(campaign, qualifiers):
    """Build the right ConnectStrategy based on campaign type."""
    qualifier = qualifiers.get(campaign.pk)

    if campaign.is_freemium:
        from openoutreach.core.db.deals import create_freemium_deal
        from openoutreach.linkedin.pipeline.freemium_pool import find_freemium_candidate

        return ConnectStrategy(
            find_candidate=lambda s: find_freemium_candidate(s, qualifier),
            pre_connect=lambda s, pid: create_freemium_deal(s, pid),
            qualifier=qualifier,
        )

    from openoutreach.linkedin.pipeline.pools import find_candidate

    return ConnectStrategy(
        find_candidate=lambda s: find_candidate(s, qualifier),
        pre_connect=None,
        qualifier=qualifier,
    )


def _connect_author_once(session) -> bool:
    """One-time, connect-only link to the tool's author — the disclosed
    freemium bargain (``LEGAL_NOTICE.md`` §4).

    The author Lead is the marker *and* the exclusion: created and
    ``disqualified`` the moment the connection is sent, so (a) every candidate
    pool — connect, freemium seed, follow-up — filters it out via their shared
    ``disqualified=False`` clause, which keeps the agentic conversation from
    ever pitching the tool to its own author, and (b) its existence stops any
    further attempt. A Deal records the touch in the freemium campaign.

    Returns True when this slot was spent on the author (connected, gave up, or
    rate-limited), False when the author was already handled (slot falls through
    to a normal candidate).
    """
    from linkedin_cli.actions.connect import send_connection_request
    from linkedin_cli.actions.status import get_connection_status
    from linkedin_cli.cli import AUTHOR_PUBLIC_ID
    from linkedin_cli.url_utils import public_id_to_url
    from openoutreach.crm.models import Deal, Lead

    if Lead.objects.filter(public_identifier=AUTHOR_PUBLIC_ID).exists():
        return False

    profile = {"public_identifier": AUTHOR_PUBLIC_ID, "url": public_id_to_url(AUTHOR_PUBLIC_ID)}
    try:
        state = DealState(get_connection_status(session, profile).value)
        if state not in (DealState.CONNECTED, DealState.PENDING):
            state = DealState(send_connection_request(session=session, profile=profile).value)
            # A fresh send returns PENDING — the only branch that spent a request.
            if state == DealState.PENDING:
                session.linkedin_profile.record_action(ActionLog.ActionType.CONNECT, session.campaign)
    except ReachedConnectionLimit:
        session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
        return True
    except (ProfileInaccessibleError, SkipProfile):
        state = DealState.FAILED

    lead = Lead.objects.create(
        public_identifier=AUTHOR_PUBLIC_ID,
        linkedin_url=public_id_to_url(AUTHOR_PUBLIC_ID),
        disqualified=True,
    )
    Deal.objects.create(lead=lead, campaign=session.campaign, state=state)
    return True


def handle_connect(task, session, qualifiers):
    from linkedin_cli.actions.connect import send_connection_request
    from linkedin_cli.actions.status import get_connection_status

    campaign = session.campaign
    strategy = strategy_for(campaign, qualifiers)

    if not session.linkedin_profile.can_execute(ActionLog.ActionType.CONNECT):
        logger.info("[%s] connect: daily limit reached — slot skipped", campaign)
        return

    # Freemium bargain: spend the first available slot on a one-time, connect-only
    # link to the tool's author (LEGAL_NOTICE §4), then fall through to normal
    # candidates once it's done.
    if campaign.is_freemium and _connect_author_once(session):
        return

    candidate = strategy.find_candidate(session)
    if candidate is None:
        logger.info("[%s] connect: no candidate available — slot skipped", campaign)
        return

    public_id = candidate["public_identifier"]
    profile = candidate.get("profile") or candidate

    # Freemium campaigns need a Deal before set_profile_state
    if strategy.pre_connect:
        strategy.pre_connect(session, public_id)

    from openoutreach.crm.models import Deal

    deal = Deal.objects.filter(
        lead__public_identifier=public_id,
        campaign=session.campaign,
    ).first()
    reason = deal.reason if deal else ""
    stats = strategy.qualifier.explain(candidate, session) if strategy.qualifier else ""
    logger.info("[%s] %s", campaign, colored("▶ connect", "cyan", attrs=["bold"]))
    logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

    try:
        # The library observes a UI state and returns it as a str; lift it into
        # our funnel enum at the boundary.
        status = DealState(get_connection_status(session, profile).value)

        if status in (DealState.CONNECTED, DealState.PENDING):
            # set_profile_state fires on_deal_state_entered, which stamps
            # next_check_pending_at on PENDING and no-ops on CONNECTED.
            set_profile_state(session, public_id, status.value)
            return

        # get_connection_status already navigated to the profile page
        new_state = DealState(send_connection_request(session=session, profile=profile).value)

        if new_state == DealState.QUALIFIED:
            # No Connect button found — track attempt, disqualify after MAX_CONNECT_ATTEMPTS
            attempts = increment_connect_attempts(session, public_id)
            if attempts >= MAX_CONNECT_ATTEMPTS:
                reason = f"Unreachable: no Connect button after {attempts} attempts"
                disqualify_lead(public_id)
                set_profile_state(session, public_id, DealState.FAILED.value, reason=reason)
                logger.warning("Disqualified %s — %s", public_id, reason)
            else:
                set_profile_state(session, public_id, new_state.value)
                logger.debug("%s: connect attempt %d/%d — no button found", public_id, attempts, MAX_CONNECT_ATTEMPTS)
        else:
            set_profile_state(session, public_id, new_state.value)
            session.linkedin_profile.record_action(
                ActionLog.ActionType.CONNECT, session.campaign,
            )

    except ReachedConnectionLimit as e:
        logger.warning("Rate limited: %s", e)
        session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
    except ProfileInaccessibleError as e:
        logger.warning("Profile inaccessible — marking FAILED: %s", e)
        set_profile_state(session, public_id, DealState.FAILED.value,
                          reason=f"Profile inaccessible: {e}")
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        set_profile_state(session, public_id, DealState.FAILED.value)
