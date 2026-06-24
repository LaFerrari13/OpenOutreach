from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_apply_persists_first_message_guidance():
    from openoutreach.core.models import Campaign
    from openoutreach.core.onboarding import OnboardConfig, apply

    Campaign.objects.all().delete()
    guidance = "Ask for candid feedback on the linked prototype."

    apply(OnboardConfig(
        campaign_name="TAMdx",
        product_description="Product details",
        campaign_objective="Collect feedback",
        first_message_guidance=guidance,
    ))

    campaign = Campaign.objects.get(name="TAMdx")
    assert campaign.first_message_guidance == guidance
