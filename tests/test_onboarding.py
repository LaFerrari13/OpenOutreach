from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_apply_persists_linkedin_message_guidance():
    from openoutreach.core.models import Campaign
    from openoutreach.core.onboarding import OnboardConfig, apply

    Campaign.objects.all().delete()
    first_guidance = "Ask whether they have two minutes to give feedback."
    positive_reply_guidance = "Explain TAMdx and share https://tamdx.sorvanis.ai."

    apply(OnboardConfig(
        campaign_name="TAMdx",
        product_description="Product details",
        campaign_objective="Collect feedback",
        first_message_guidance=first_guidance,
        positive_reply_guidance=positive_reply_guidance,
    ))

    campaign = Campaign.objects.get(name="TAMdx")
    assert campaign.first_message_guidance == first_guidance
    assert campaign.positive_reply_guidance == positive_reply_guidance
