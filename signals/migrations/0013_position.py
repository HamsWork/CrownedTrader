from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("signals", "0012_backfill_trade_plan_tp_mode"),
    ]

    operations = [
        migrations.CreateModel(
            name="Position",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("status", models.CharField(choices=[("open", "Open"), ("closed", "Closed")], default="open", max_length=10)),
                ("symbol", models.CharField(blank=True, default="", max_length=20)),
                ("instrument", models.CharField(choices=[("options", "Options"), ("shares", "Shares")], default="options", max_length=12)),
                ("option_contract", models.CharField(blank=True, default="", max_length=64)),
                ("option_type", models.CharField(blank=True, default="", max_length=10)),
                ("strike", models.CharField(blank=True, default="", max_length=32)),
                ("expiration", models.CharField(blank=True, default="", max_length=32)),
                ("quantity", models.IntegerField(default=1)),
                ("multiplier", models.IntegerField(default=100)),
                ("entry_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("exit_price", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "signal",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="positions", to="signals.signal"),
                ),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="positions", to="auth.user"),
                ),
            ],
            options={
                "ordering": ["-opened_at", "-created_at"],
            },
        ),
    ]

