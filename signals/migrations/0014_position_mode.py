from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("signals", "0013_position"),
    ]

    operations = [
        migrations.AddField(
            model_name="position",
            name="mode",
            field=models.CharField(
                choices=[("auto", "Automatic"), ("manual", "Manual")],
                default="manual",
                max_length=10,
            ),
        ),
    ]

