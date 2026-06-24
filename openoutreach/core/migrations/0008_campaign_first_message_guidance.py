from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_siteconfig_provider_model_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="campaign",
            name="first_message_guidance",
            field=models.TextField(blank=True, default=""),
            preserve_default=False,
        ),
    ]
