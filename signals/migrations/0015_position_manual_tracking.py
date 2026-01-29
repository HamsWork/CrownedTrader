from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("signals", "0014_position_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="position",
            name="tp_hit_level",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="position",
            name="sl_hit",
            field=models.BooleanField(default=False),
        ),
    ]

