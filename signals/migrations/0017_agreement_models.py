from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings


class Migration(migrations.Migration):

    dependencies = [
        ("signals", "0016_position_realized_pnl"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Agreement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.CharField(max_length=40, unique=True)),
                ("title", models.CharField(default="Crowned Trader Agreement", max_length=120)),
                ("body", models.TextField(default="")),
                ("is_active", models.BooleanField(default=True)),
                ("published_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-is_active", "-published_at", "-id"],
            },
        ),
        migrations.CreateModel(
            name="AgreementAcceptance",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("accepted_at", models.DateTimeField(auto_now_add=True)),
                ("agreement", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="acceptances", to="signals.agreement")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="agreement_acceptances", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-accepted_at", "-id"],
                "unique_together": {("agreement", "user")},
            },
        ),
    ]

