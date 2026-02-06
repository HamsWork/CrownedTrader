# Generated migration: add Chart Analysis optional field to Trade Alert

from django.db import migrations


def add_chart_analysis_to_trade_alert(apps, schema_editor):
    """Add Chart Analysis variable and optional field to Trade Alert (Common Trade Alert)."""
    SignalType = apps.get_model("signals", "SignalType")
    st = SignalType.objects.filter(user__isnull=True, name="Trade Alert").first()
    if not st:
        return

    variables = list(st.variables or [])
    # Avoid duplicate
    if any(v.get("name") == "chart_analysis" for v in variables if isinstance(v, dict)):
        return

    variables.append({
        "name": "chart_analysis",
        "type": "file",
        "label": "Chart Analysis",
        "required": False,
    })
    st.variables = variables

    fields = list(st.fileds_template or [])
    # Avoid duplicate
    if any("Chart Analysis" in (f.get("name") or "") for f in fields if isinstance(f, dict)):
        st.save(update_fields=["variables"])
        return

    fields.append({
        "name": "📊 Chart Analysis",
        "value": "{{chart_analysis}}",
        "inline": False,
        "optional": True,
    })
    st.fileds_template = fields
    st.save(update_fields=["variables", "fileds_template"])


def remove_chart_analysis_from_trade_alert(apps, schema_editor):
    """Reverse: remove Chart Analysis variable and field."""
    SignalType = apps.get_model("signals", "SignalType")
    st = SignalType.objects.filter(user__isnull=True, name="Trade Alert").first()
    if not st:
        return

    variables = [v for v in (st.variables or []) if not (isinstance(v, dict) and v.get("name") == "chart_analysis")]
    fields = [f for f in (st.fileds_template or []) if "Chart Analysis" not in (f.get("name") or "")]
    st.variables = variables
    st.fileds_template = fields
    st.save(update_fields=["variables", "fileds_template"])


class Migration(migrations.Migration):
    dependencies = [
        ("signals", "0017_agreement_models"),
    ]

    operations = [
        migrations.RunPython(add_chart_analysis_to_trade_alert, remove_chart_analysis_from_trade_alert),
    ]
