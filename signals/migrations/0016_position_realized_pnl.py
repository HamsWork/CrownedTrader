from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("signals", "0015_position_manual_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="position",
            name="closed_units",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="position",
            name="realized_pnl",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
    ]

