from django.contrib import admin

from openoutreach.crm.models import Deal, Lead


@admin.register(Lead)
class LeadAdmin(admin.ModelAdmin):
    list_display = (
        "public_identifier",
        "linkedin_url",
        "country_code",
        "api_email",
        "disqualified",
        "creation_date",
        "update_date",
    )
    list_filter = ("disqualified", "country_code")
    search_fields = ("public_identifier", "linkedin_url", "urn", "api_email")
    readonly_fields = ("creation_date", "update_date")
    ordering = ("-update_date",)


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = (
        "lead",
        "campaign",
        "state",
        "outcome",
        "mailbox",
        "email_sent_at",
        "creation_date",
        "update_date",
    )
    list_filter = ("state", "outcome", "campaign", "mailbox")
    search_fields = (
        "lead__public_identifier",
        "lead__linkedin_url",
        "lead__api_email",
        "campaign__name",
        "email_subject",
    )
    raw_id_fields = ("lead", "campaign", "mailbox")
    readonly_fields = ("creation_date", "update_date", "email_sent_at", "email_message_id")
    date_hierarchy = "creation_date"
    ordering = ("-update_date",)
