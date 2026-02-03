# Ensure "Trade Alert" and "Common Trade Alert" have Optional Fields: Risk Management, Chart Analysis

from django.db import migrations


def _ensure_optional_fields(apps, name):
    """Ensure signal type has Risk Management and Chart Analysis as optional (variables + fields)."""
    SignalType = apps.get_model("signals", "SignalType")
    st = SignalType.objects.filter(user__isnull=True, name=name).first()
    if not st:
        return

    variables = list(st.variables or [])
    changed_vars = False

    # Ensure risk_management variable exists (type text)
    if not any(isinstance(v, dict) and v.get("name") == "risk_management" for v in variables):
        variables.append({
            "name": "risk_management",
            "type": "text",
            "label": "Risk Management",
            "required": False,
            "hint": "Enter Risk Management (e.g. position size, 0DTE rules, due diligence)",
        })
        changed_vars = True

    # Ensure chart_analysis variable exists (type file)
    if not any(isinstance(v, dict) and v.get("name") == "chart_analysis" for v in variables):
        variables.append({
            "name": "chart_analysis",
            "type": "file",
            "label": "Chart Analysis",
            "required": False,
        })
        changed_vars = True

    if changed_vars:
        st.variables = variables

    fields = list(st.fileds_template or [])
    changed_fields = False

    # Ensure Risk Management field exists and is optional
    has_risk_mgmt = any(
        isinstance(f, dict) and ("risk_management" in (f.get("value") or "") or "Risk Management" in (f.get("name") or ""))
        for f in fields
    )
    if not has_risk_mgmt:
        fields.append({
            "name": "⚠️ Risk Management",
            "value": "{{risk_management}}",
            "inline": False,
            "optional": True,
        })
        changed_fields = True
    else:
        for f in fields:
            if isinstance(f, dict) and ("risk_management" in (f.get("value") or "") or "Risk Management" in (f.get("name") or "")):
                f["optional"] = True
                changed_fields = True
                break

    # Ensure Chart Analysis field exists and is optional
    has_chart = any(isinstance(f, dict) and "Chart Analysis" in (f.get("name") or "") for f in fields)
    if not has_chart:
        fields.append({
            "name": "📊 Chart Analysis",
            "value": "{{chart_analysis}}",
            "inline": False,
            "optional": True,
        })
        changed_fields = True
    else:
        for f in fields:
            if isinstance(f, dict) and "Chart Analysis" in (f.get("name") or ""):
                f["optional"] = True
                changed_fields = True
                break

    if changed_vars or changed_fields:
        if changed_vars:
            st.variables = variables
        if changed_fields:
            st.fileds_template = fields
        st.save(update_fields=["variables", "fileds_template"])


def add_optional_fields_common_trade_alert(apps, schema_editor):
    """Add Optional Fields (Risk Management, Chart Analysis) to Trade Alert and Common Trade Alert."""
    for name in ("Trade Alert", "Common Trade Alert"):
        _ensure_optional_fields(apps, name)


def noop_reverse(apps, schema_editor):
    """No reverse - optional fields can stay."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("signals", "0018_trade_alert_chart_analysis"),
    ]

    operations = [
        migrations.RunPython(add_optional_fields_common_trade_alert, noop_reverse),
    ]
