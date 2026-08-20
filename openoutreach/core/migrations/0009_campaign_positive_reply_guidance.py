from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_campaign_first_message_guidance"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="positive_reply_guidance",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
    ]
