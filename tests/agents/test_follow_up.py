"""Tests for the follow-up agent context builder + Jinja template."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from tests.factories import LeadFactory, DealFactory


@pytest.fixture
def deal_with_summaries(db, fake_session):
    lead = LeadFactory(public_identifier="alice")
    return DealFactory(
        lead=lead,
        campaign=fake_session.campaign,
        profile_summary={"facts": [
            "Senior engineer at Acme Corp.",
            "Based in Berlin, Germany.",
            "Speaks English and German.",
        ]},
        chat_summary={"facts": [
            "Lead is curious about pricing.",
            "Lead has a small team budget.",
        ]},
    )


def _msg(content, is_outgoing):
    m = MagicMock()
    m.content = content
    m.is_outgoing = is_outgoing
    m.creation_date = None
    return m


class TestRenderSystemPrompt:
    def test_includes_three_summary_blocks(self, db, fake_session, deal_with_summaries):
        from openoutreach.core.agents.follow_up import _render_system_prompt

        # Stub session.self_profile so the prompt builder works without a browser.
        fake_session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}

        recent = [_msg("Hi, what do you do?", is_outgoing=True), _msg("Sales tooling.", is_outgoing=False)]
        prompt = _render_system_prompt(fake_session, deal_with_summaries, recent)

        # Profile facts appear under the lead-knowledge block.
        assert "Senior engineer at Acme Corp." in prompt
        assert "Based in Berlin, Germany." in prompt
        # Chat facts appear under the conversation-knowledge block.
        assert "Lead is curious about pricing." in prompt
        # Verbatim recent messages appear in Me:/Lead: format.
        assert "Me: Hi, what do you do?" in prompt
        assert "Lead: Sales tooling." in prompt
        # The legacy flat fields are gone.
        assert "Headline:" not in prompt
        assert "Company:" not in prompt

    def test_handles_missing_summaries_gracefully(self, db, fake_session):
        from openoutreach.core.agents.follow_up import _render_system_prompt

        lead = LeadFactory(public_identifier="bob")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)
        fake_session.self_profile = {"first_name": "Bob", "last_name": "Builder", "urn": "urn:li:fsd_profile:SELF"}

        prompt = _render_system_prompt(fake_session, deal, [])

        # Renders without crashing and shows the empty placeholders.
        assert "(none yet)" in prompt
        assert "No recent messages." in prompt

    def test_includes_campaign_guidance_for_empty_conversation(self, db, fake_session):
        from openoutreach.core.agents.follow_up import _render_system_prompt

        guidance = "Ask for candid feedback at https://tamdx.sorvanis.ai."
        fake_session.campaign.first_message_guidance = guidance
        fake_session.campaign.save(update_fields=["first_message_guidance"])
        lead = LeadFactory(public_identifier="bob")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)

        prompt = _render_system_prompt(fake_session, deal, [])

        assert "## First Message Guidance" in prompt
        assert guidance in prompt
        assert "near-template, not a topic brief" in prompt
        assert "overrides the Mom Test/profile-context strategy" in prompt
        assert "Do NOT personalize the opener around the lead's profile" in prompt
        assert "Do NOT open with discovery" in prompt
        assert '"live demo", "platform", "solution", or "AI tool"' in prompt
        assert "as a close variation, not a profile-personalized discovery opener" in prompt

    def test_omits_campaign_guidance_after_messages_exist(self, db, fake_session):
        from openoutreach.core.agents.follow_up import _render_system_prompt

        guidance = "Ask for candid feedback at https://tamdx.sorvanis.ai."
        fake_session.campaign.first_message_guidance = guidance
        fake_session.campaign.save(update_fields=["first_message_guidance"])
        lead = LeadFactory(public_identifier="bob")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)

        prompt = _render_system_prompt(fake_session, deal, [_msg("Hello", is_outgoing=False)])

        assert "## First Message Guidance" not in prompt
        assert guidance not in prompt

    def test_two_message_opener_uses_name_and_withholds_product(self, db, fake_session):
        from openoutreach.core.agents.follow_up import _render_system_prompt

        campaign = fake_session.campaign
        campaign.product_docs = "TAMdx product details that must not appear in the opener prompt."
        campaign.campaign_objective = "Pitch TAMdx to this lead."
        campaign.booking_link = "https://cal.example.com/tamdx"
        campaign.first_message_guidance = (
            "hi {name}, i'm building something and think you can provide valuable feedback. "
            "do you have 2 mins to check it out?"
        )
        campaign.positive_reply_guidance = (
            "This is TAMdx. See https://tamdx.sorvanis.ai and share your honest feedback."
        )
        campaign.save(update_fields=[
            "product_docs", "campaign_objective",
            "booking_link", "first_message_guidance", "positive_reply_guidance",
        ])
        deal = DealFactory(
            lead=LeadFactory(public_identifier="alice"),
            campaign=campaign,
            profile_summary={"facts": ["Alice works in growth."], "first_name": "Alice"},
        )

        prompt = _render_system_prompt(fake_session, deal, [])

        assert "This campaign uses a two-message permission strategy" in prompt
        assert "The lead's first name is `Alice`" in prompt
        assert "Do NOT mention the product name, product details, pitch, demo, or any URL" in prompt
        assert "## Positive Reply Guidance" not in prompt
        assert campaign.positive_reply_guidance not in prompt
        assert "stop after asking permission" in prompt
        assert campaign.product_docs not in prompt
        assert campaign.campaign_objective not in prompt
        assert campaign.booking_link not in prompt
        assert "You follow the Mom Test method" not in prompt
        assert "replaces the normal discovery" in prompt
        assert "strategy for this stage" in prompt
        assert "Use the language of the active campaign message guidance" in prompt

    def test_positive_first_reply_includes_second_near_template(self, db, fake_session):
        from django.utils import timezone
        from openoutreach.chat.models import ChatMessage
        from openoutreach.core.agents.follow_up import _load_recent_messages, _render_system_prompt

        campaign = fake_session.campaign
        campaign.first_message_guidance = "Ask permission first."
        campaign.positive_reply_guidance = (
            "This is TAMdx. Demo: https://tamdx.sorvanis.ai. I only need honest feedback."
        )
        campaign.save(update_fields=["first_message_guidance", "positive_reply_guidance"])
        deal = DealFactory(lead=LeadFactory(public_identifier="alice"), campaign=campaign)
        ChatMessage.objects.create(
            deal=deal, content="Do you have two minutes to check it out?", is_outgoing=True,
            owner=fake_session.django_user, linkedin_urn="urn:opener", creation_date=timezone.now(),
        )
        ChatMessage.objects.create(
            deal=deal, content="Sure, send it over.", is_outgoing=False,
            owner=fake_session.django_user, linkedin_urn="urn:reply", creation_date=timezone.now(),
        )

        prompt = _render_system_prompt(fake_session, deal, _load_recent_messages(deal))

        assert "## First Message Guidance" not in prompt
        assert "## Positive Reply Guidance" in prompt
        assert campaign.positive_reply_guidance in prompt
        assert "If the reply declines, is hesitant without granting permission, or is unrelated" in prompt
        assert "overrides the normal 1-3 sentence limit" in prompt
        assert "Apply the positive-reply gate" in prompt

    def test_second_guidance_is_removed_after_second_outgoing(self, db, fake_session):
        from datetime import timedelta
        from django.utils import timezone
        from openoutreach.chat.models import ChatMessage
        from openoutreach.core.agents.follow_up import _load_recent_messages, _render_system_prompt

        campaign = fake_session.campaign
        campaign.first_message_guidance = "Ask permission first."
        campaign.positive_reply_guidance = "Explain TAMdx and share the demo."
        campaign.save(update_fields=["first_message_guidance", "positive_reply_guidance"])
        deal = DealFactory(lead=LeadFactory(public_identifier="alice"), campaign=campaign)
        base = timezone.now()
        turns = [
            ("Can I show you something?", True),
            ("Sure.", False),
            ("This is TAMdx.", True),
            ("How does it find companies?", False),
        ]
        for i, (content, outgoing) in enumerate(turns):
            ChatMessage.objects.create(
                deal=deal, content=content, is_outgoing=outgoing,
                owner=fake_session.django_user, linkedin_urn=f"urn:turn:{i}",
                creation_date=base + timedelta(minutes=i),
            )

        prompt = _render_system_prompt(fake_session, deal, _load_recent_messages(deal))

        assert "## Positive Reply Guidance" not in prompt
        assert campaign.positive_reply_guidance not in prompt
        assert "Respond contextually to the literal phrasing of the last message" in prompt


class TestLoadRecentMessages:
    def test_returns_last_n_in_chronological_order(self, db, fake_session):
        from openoutreach.chat.models import ChatMessage
        from django.utils import timezone
        from datetime import timedelta

        from openoutreach.core.agents.follow_up import _load_recent_messages, RECENT_MESSAGES_WINDOW

        lead = LeadFactory(public_identifier="alice")
        deal = DealFactory(lead=lead, campaign=fake_session.campaign)

        base = timezone.now()
        for i in range(RECENT_MESSAGES_WINDOW + 3):
            ChatMessage.objects.create(
                deal=deal,
                content=f"msg-{i}",
                is_outgoing=(i % 2 == 0),
                owner=fake_session.django_user,
                linkedin_urn=f"urn:msg:{i}",
                creation_date=base + timedelta(minutes=i),
            )

        recent = _load_recent_messages(deal)

        # Window respected and chronological order preserved.
        assert len(recent) == RECENT_MESSAGES_WINDOW
        contents = [m.content for m in recent]
        assert contents == sorted(contents, key=lambda c: int(c.split("-")[1]))
        # Returned the *latest* messages.
        assert contents[-1] == f"msg-{RECENT_MESSAGES_WINDOW + 2}"
