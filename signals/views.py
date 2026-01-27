from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordChangeForm
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.views.decorators.http import require_http_methods
from django.db.models import Q
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
from datetime import datetime
import logging
import requests
import re
import json
import html
from .forms import SignalForm, SignalTypeForm
from .models import Signal, SignalType, UserProfile, DiscordChannel, UserTradePlan, UserTradePlanPreset
from .tickers import get_us_tickers
from .polygon_client import PolygonClient

logger = logging.getLogger(__name__)

TRADINGVIEW_SYMBOL_SEARCH_URL = "https://symbol-search.tradingview.com/symbol_search/"
US_STOCK_EXCHANGES = {"NASDAQ", "NYSE", "AMEX"}
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _tv_headers():
    return {
        "Accept": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
    }


def _normalize_exchange(ex: str) -> str:
    ex = (ex or "").strip().upper()
    # TradingView sometimes returns variants like "NasdaqNM" etc; best-effort normalize.
    if "NASDAQ" in ex:
        return "NASDAQ"
    if "NYSE" in ex:
        return "NYSE"
    if "AMEX" in ex or "NYSEAMERICAN" in ex or "NYSE ARCA" in ex or "ARCA" in ex:
        return "AMEX"
    return ex


def _strip_html(s: str) -> str:
    """
    TradingView symbol search can include highlighted HTML like <em>...</em>.
    Return plain text.
    """
    s = str(s or "")
    if not s:
        return ""
    s = _HTML_TAG_RE.sub("", s)
    s = html.unescape(s)
    return s.strip()


def _search_tickers_tradingview(q: str, *, limit: int, include_etfs: bool) -> list[dict]:
    """
    Live symbol search via TradingView.
    Returns list[{symbol, name}] limited and filtered to US exchanges.
    """
    q = (q or "").strip()
    if not q:
        return []

    # TradingView returns mixed asset classes; filter down to stock/etf on US exchanges.
    allowed_types = {"stock"}
    if include_etfs:
        allowed_types.add("etf")

    params = {
        "text": q,
        # Disable HTML highlight markup (otherwise descriptions may contain <em> tags)
        "hl": "0",
        "lang": "en",
        "domain": "production",
    }
    # Keep this fast; UI is debounced and will retry as user types.
    resp = requests.get(
        TRADINGVIEW_SYMBOL_SEARCH_URL,
        params=params,
        headers=_tv_headers(),
        timeout=6,
    )
    resp.raise_for_status()
    payload = resp.json()

    results = []
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            sym = _strip_html(item.get("symbol") or item.get("name") or "").upper()
            desc = _strip_html(item.get("description") or item.get("full_name") or "")
            typ = str(item.get("type") or "").strip().lower()
            ex = _normalize_exchange(str(item.get("exchange") or ""))

            if not sym or typ not in allowed_types:
                continue
            if ex not in US_STOCK_EXCHANGES:
                continue

            results.append({"symbol": sym, "name": desc})

    # Prefer prefix matches on symbol; then alphabetical.
    q_upper = q.upper()
    results.sort(key=lambda r: (0 if r["symbol"].startswith(q_upper) else 1, r["symbol"]))
    if limit:
        results = results[:limit]
    return results


def _normalize_symbol(symbol: str) -> str:
    symbol = (symbol or "").strip().upper()
    # Basic allowlist: alnum plus "." and "-" (BRK.B, BF.B, etc)
    if not symbol:
        return ""
    if not all(ch.isalnum() or ch in ".-" for ch in symbol):
        return ""
    return symbol

DISCORD_EMBED_TITLE_MAX_CHARS = 256
DISCORD_EMBED_DESCRIPTION_MAX_CHARS = 4096
DISCORD_EMBED_FIELD_NAME_MAX_CHARS = 256
DISCORD_EMBED_FIELD_VALUE_MAX_CHARS = 1024
DISCORD_EMBED_FOOTER_MAX_CHARS = 2048
DISCORD_EMBED_MAX_FIELDS = 25
DISCORD_EMBED_MAX_TOTAL_CHARS = 6000


def _coerce_to_str(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    return str(value)


def calculate_embed_length(embed):
    """Calculate the total character count for a Discord embed."""
    if not embed:
        return 0

    total = 0
    total += len(_coerce_to_str(embed.get('title')))
    total += len(_coerce_to_str(embed.get('description')))

    footer = embed.get('footer') or {}
    total += len(_coerce_to_str(footer.get('text')))

    for field in embed.get('fields') or []:
        total += len(_coerce_to_str(field.get('name')))
        total += len(_coerce_to_str(field.get('value')))

    return total


def validate_embed(embed):
    """Validate Discord embed against known length limitations."""
    if not embed:
        return True, 0, None

    total = 0

    title = _coerce_to_str(embed.get('title'))
    title_len = len(title)
    if title_len > DISCORD_EMBED_TITLE_MAX_CHARS:
        return False, total + title_len, (
            f"Discord embed title is {title_len} characters (max {DISCORD_EMBED_TITLE_MAX_CHARS})."
        )
    total += title_len

    description = _coerce_to_str(embed.get('description'))
    description_len = len(description)
    if description_len > DISCORD_EMBED_DESCRIPTION_MAX_CHARS:
        return False, total + description_len, (
            f"Discord embed description is {description_len} characters (max {DISCORD_EMBED_DESCRIPTION_MAX_CHARS})."
        )
    total += description_len

    fields = embed.get('fields') or []
    if len(fields) > DISCORD_EMBED_MAX_FIELDS:
        return False, total, (
            f"Discord embed has {len(fields)} fields (max {DISCORD_EMBED_MAX_FIELDS})."
        )

    for index, field in enumerate(fields, start=1):
        name = _coerce_to_str(field.get('name'))
        value = _coerce_to_str(field.get('value'))
        name_len = len(name)
        value_len = len(value)
        field_label = name or f"Field {index}"

        if name_len > DISCORD_EMBED_FIELD_NAME_MAX_CHARS:
            return False, total + name_len, (
                f"Discord field '{field_label}' name is {name_len} characters "
                f"(max {DISCORD_EMBED_FIELD_NAME_MAX_CHARS})."
            )

        if value_len > DISCORD_EMBED_FIELD_VALUE_MAX_CHARS:
            return False, total + name_len + value_len, (
                f"Discord field '{field_label}' value is {value_len} characters "
                f"(max {DISCORD_EMBED_FIELD_VALUE_MAX_CHARS})."
            )

        total += name_len + value_len

    footer = embed.get('footer') or {}
    footer_text = _coerce_to_str(footer.get('text'))
    footer_len = len(footer_text)
    if footer_len > DISCORD_EMBED_FOOTER_MAX_CHARS:
        return False, total + footer_len, (
            f"Discord embed footer is {footer_len} characters (max {DISCORD_EMBED_FOOTER_MAX_CHARS})."
        )
    total += footer_len

    if total > DISCORD_EMBED_MAX_TOTAL_CHARS:
        return False, total, (
            f"Discord embed content is {total} characters (max {DISCORD_EMBED_MAX_TOTAL_CHARS})."
        )

    return True, total, None

def send_to_discord(signal):
    """Send signal data to Discord channel using user's webhook"""
    
    # Get the appropriate template based on signal type
    embed = get_signal_template(signal)

    is_valid, _, validation_error = validate_embed(embed)
    if not is_valid:
        print(f"ERROR: Discord embed validation failed - {validation_error}")
        return False
    
    payload = {
        "embeds": [embed]
    }
    
    # Try to get user's webhook - check for selected channel or default channel
    try:
        # Check if signal has a selected discord_channel
        if signal.discord_channel and signal.discord_channel.is_active:
            url = signal.discord_channel.webhook_url
        else:
            # Try to get default channel
            default_channel = DiscordChannel.objects.filter(user=signal.user, is_default=True, is_active=True).first()
            if default_channel:
                url = default_channel.webhook_url
            else:
                # Fallback to first active channel
                first_channel = DiscordChannel.objects.filter(user=signal.user, is_active=True).first()
                if first_channel:
                    url = first_channel.webhook_url
                else:
                    # Fallback to old UserProfile webhook for backward compatibility
                    try:
                        user_profile = signal.user.profile
                        if user_profile and user_profile.discord_channel_webhook:
                            url = user_profile.discord_channel_webhook
                        else:
                            print(f"ERROR: User {signal.user.username} does not have a Discord webhook configured")
                            return False
                    except UserProfile.DoesNotExist:
                        print(f"ERROR: User {signal.user.username} does not have a profile or Discord channels")
                        return False
        
        headers = {
            "Content-Type": "application/json"
        }
    except Exception as e:
        print(f"ERROR: Failed to get Discord webhook: {e}")
        return False
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return True
    except requests.HTTPError as e:
        error_msg = response.json() if response.text else {}
        print(f"Failed to send to Discord: {e}")
        print(f"Status: {response.status_code}")
        print(f"Response: {error_msg}")
        
        # Provide specific guidance based on status code
        if response.status_code == 401:
            print("ERROR: Invalid webhook URL")
        elif response.status_code == 403:
            print("ERROR: Webhook lacks permissions")
        elif response.status_code == 404:
            print("ERROR: Webhook not found")
        elif response.status_code == 400:
            print("ERROR: Invalid webhook URL or bad request")
        
        return False
    except requests.RequestException as e:
        print(f"Failed to send to Discord: {e}")
        return False

def hex_to_int(color_hex):
    """Convert hex color string to integer"""
    try:
        # Remove # if present
        color_hex = color_hex.lstrip('#')
        return int(color_hex, 16)
    except (ValueError, AttributeError):
        return 0x808080  # Default gray

def _get_stock_price(symbol: str, quote_cache=None):
    """
    Best-effort stock price lookup for template modifiers like {{ticker::stock_price}}.
    Prefers PolygonClient (if POLYGON_API_KEY is set), falls back to Yahoo quote.
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return None

    cache = quote_cache if isinstance(quote_cache, dict) else None
    if cache is not None and sym in cache:
        return cache[sym]

    price = None

    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    if polygon_key:
        try:
            client = PolygonClient(polygon_key)
            q = client.get_latest_quote(sym)
            if q and q.get("p") is not None:
                price = float(q["p"])
        except Exception:
            price = None

    # NOTE: Intentionally no Yahoo fallback here.
    # If Polygon is unavailable (missing key, entitlement, rate limit), return None.

    if cache is not None:
        cache[sym] = price

    return price


def _get_company_name(symbol: str, info_cache=None) -> str:
    """
    Best-effort company name lookup for template modifiers like {{ticker::company_name}}.
    Polygon-only (returns empty string if unavailable).
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return ""

    cache = info_cache if isinstance(info_cache, dict) else None
    key = f"name:{sym}"
    if cache is not None and key in cache:
        return cache[key] or ""

    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    name = ""
    if polygon_key:
        try:
            client = PolygonClient(polygon_key)
            name = client.get_company_name(sym) or ""
        except Exception:
            name = ""

    if cache is not None:
        cache[key] = name

    return name


def render_template(template_string, variables, quote_cache=None):
    """
    Render template string by replacing {{variable}} placeholders with actual values.

    Supports modifiers:
      - {{ticker::stock_price}} -> current stock price for the ticker symbol
    """
    if not template_string:
        return ""

    def replace_var(match):
        var_name = (match.group(1) or "").strip()
        modifier = (match.group(2) or "").strip()

        if modifier == "stock_price":
            # Convention: ticker variable holds a symbol string.
            symbol = variables.get(var_name, "") if isinstance(variables, dict) else ""
            price = _get_stock_price(str(symbol or ""), quote_cache=quote_cache)
            # Default when unavailable: 0.000
            return f"{price:.3f}" if isinstance(price, (int, float)) else "0.000"

        if modifier == "company_name":
            symbol = variables.get(var_name, "") if isinstance(variables, dict) else ""
            return _get_company_name(str(symbol or ""), info_cache=quote_cache)

        # Convenience: allow "namespaced" access like {{ticker::strike}} meaning {{strike}}.
        # (The base name is ignored; modifier is treated as the target variable.)
        if modifier in (
            "is_shares",
            "strike",
            "expiration",
            "option_type",
            "option_price",
            "tp1_mode",
            "tp2_mode",
            "tp3_mode",
            "tp4_mode",
            "tp5_mode",
            "tp6_mode",
            "tp1_per",
            "tp2_per",
            "tp3_per",
            "tp4_per",
            "tp5_per",
            "tp6_per",
            "tp1_stock_price",
            "tp2_stock_price",
            "tp3_stock_price",
            "tp4_stock_price",
            "tp5_stock_price",
            "tp6_stock_price",
            "tp1_takeoff_per",
            "tp2_takeoff_per",
            "tp3_takeoff_per",
            "tp4_takeoff_per",
            "tp5_takeoff_per",
            "tp6_takeoff_per",
            "sl_per",
            "tp1_price",
            "tp2_price",
            "tp3_price",
            "tp4_price",
            "tp5_price",
            "tp6_price",
            "sl_price",
        ):
            val = variables.get(modifier, "") if isinstance(variables, dict) else ""
            if modifier in ("option_price", "tp1_price", "tp2_price", "tp3_price", "tp4_price", "tp5_price", "tp6_price", "sl_price"):
                try:
                    return f"{float(val):.3f}"
                except Exception:
                    return "0.000"
            if modifier in ("tp1_stock_price", "tp2_stock_price", "tp3_stock_price", "tp4_stock_price", "tp5_stock_price", "tp6_stock_price"):
                try:
                    s = str(val).strip()
                    if not s:
                        return ""
                    return f"{float(s):.2f}"
                except Exception:
                    return str(val) if val is not None else ""
            if modifier in (
                "tp1_per",
                "tp2_per",
                "tp3_per",
                "tp4_per",
                "tp5_per",
                "tp6_per",
                "tp1_takeoff_per",
                "tp2_takeoff_per",
                "tp3_takeoff_per",
                "tp4_takeoff_per",
                "tp5_takeoff_per",
                "tp6_takeoff_per",
                "sl_per",
            ):
                s = str(val).strip() if val is not None else ""
                if not s:
                    return "0%"
                return s if s.endswith("%") else f"{s}%"
            return str(val) if val is not None else ""

        return str(variables.get(var_name, "")) if isinstance(variables, dict) else ""

    # Replace {{variable}} and {{variable::modifier}} patterns
    return re.sub(r"\{\{(\w+)(?:::(\w+))?\}\}", replace_var, template_string)

def render_fields_template(fields_template, variables, optional_fields_indices=None, quote_cache=None):
    """Render fields template from JSONField
    
    Args:
        fields_template: List of field templates
        variables: Dictionary of variable values for template rendering
        optional_fields_indices: List of indices of optional fields that should be included (default: None, includes all)
    """
    if not fields_template:
        return []
    
    # Convert optional_fields_indices to set for faster lookup
    optional_indices_set = set(optional_fields_indices) if optional_fields_indices else None
    
    # First pass: determine which fields will be included (for blank spacer detection)
    included_indices = set()
    for index, field in enumerate(fields_template):
        # Skip optional fields that are not in the selected indices
        if field.get('optional', False):
            if optional_indices_set is None or index not in optional_indices_set:
                continue  # Skip this optional field
        included_indices.add(index)
    
    rendered_fields = []
    for index, field in enumerate(fields_template):
        # Skip optional fields that are not in the selected indices
        if field.get('optional', False):
            if optional_indices_set is None or index not in optional_indices_set:
                continue  # Skip this optional field
        
        rendered_field = {
            "name": render_template(field.get('name', ''), variables, quote_cache=quote_cache),
            "value": render_template(field.get('value', ''), variables, quote_cache=quote_cache),
            "inline": field.get('inline', False),
            "optional": field.get('optional', False)
        }
        # Clean up the rendered values
        rendered_name = rendered_field["name"].strip() if rendered_field["name"] else ""
        rendered_value = rendered_field["value"].strip() if rendered_field["value"] else ""
        
        # Skip field only if BOTH name and value are empty after rendering
        # This allows for:
        # 1. Spacer fields: {'name': '', 'value': '\u200b'} - have value, so included
        # 2. Label-only fields: {'name': 'Label', 'value': ''} - have name, so included
        # 3. Variable-in-name fields: {'name': 'Target: {{targets}}', 'value': ''} - have name, so included
        # 4. Empty variable fields: variable not set results in empty name+value - excluded
        if rendered_name or rendered_value:
            if rendered_name == "" and field.get('name', ''):
                continue
            if rendered_value == "" and field.get('value', ''):
                continue
            rendered_fields.append(rendered_field)
    
    # Remove consecutive blank spacers (keep only one)
    filtered_fields = []
    prev_was_blank = False
    for field in rendered_fields:
        is_blank = (not field["name"].strip() and 
                   (not field["value"].strip() or field["value"].strip() == '\u200b'))
        if is_blank and prev_was_blank:
            continue  # Skip consecutive blank spacers
        prev_was_blank = is_blank
        filtered_fields.append(field)
    
    # Remove trailing blank spacers
    while filtered_fields:
        last_field = filtered_fields[-1]
        if not last_field["name"].strip() and (not last_field["value"].strip() or last_field["value"].strip() == '\u200b'):
            filtered_fields.pop()
        else:
            break
    
    return filtered_fields

def get_signal_template(signal):
    """Generate Discord embed template from signal"""
    signal_type = signal.signal_type
    data = signal.data or {}
    
    # Extract optional field indices and visibility flags from data (if present) - make a copy to avoid modifying original
    data_copy = dict(data)
    optional_fields_indices = data_copy.pop('_optional_fields', None)
    show_title = data_copy.pop('_show_title', True)  # Default to True if not specified
    show_description = data_copy.pop('_show_description', True)  # Default to True if not specified
    
    # Convert color hex to int
    color_int = hex_to_int(signal_type.color)
    
    # Render templates
    quote_cache = {}
    title = render_template(signal_type.title_template, data_copy, quote_cache=quote_cache) if show_title else None
    description = render_template(signal_type.description_template, data_copy, quote_cache=quote_cache) if show_description else None
    footer = render_template(signal_type.footer_template, data_copy, quote_cache=quote_cache)
    
    # Render fields with optional field filtering
    fields = render_fields_template(signal_type.fileds_template, data_copy, optional_fields_indices, quote_cache=quote_cache)

    # Inject Trade Plan block (spacer + Trade Plan + Targets + Stop Loss) into the actual Discord embed.
    # This mirrors the dashboard preview behavior, so users don't need to bake it into every template.
    def _is_truthy(v) -> bool:
        if isinstance(v, bool):
            return v
        s = str(v or "").strip().lower()
        return s in ("1", "true", "yes", "y", "on")

    is_shares = _is_truthy(data_copy.get("is_shares", False))
    if not is_shares and isinstance(fields, list):
        # Avoid duplicates if the template already includes a plan section
        def _has_plan(existing_fields: list) -> bool:
            for f in existing_fields:
                n = str((f or {}).get("name") or "").lower()
                v = str((f or {}).get("value") or "").lower()
                if "trade plan" in n or "trade plan" in v:
                    return True
                if "targets" in n or "targets" in v:
                    return True
                if "stop loss" in n or "stop loss" in v:
                    return True
            return False

        if not _has_plan(fields):
            max_tp = 6
            tps = []
            for i in range(1, max_tp + 1):
                mode = str(data_copy.get(f"tp{i}_mode") or "").strip().lower()
                stock_raw = data_copy.get(f"tp{i}_stock_price")

                is_stock = mode in ("stock", "stock_price", "underlying", "share_price") or (
                    stock_raw is not None and str(stock_raw).strip() != "" and not str(data_copy.get(f"tp{i}_per") or "").strip()
                )

                takeoff = str(data_copy.get(f"tp{i}_takeoff_per") or "").strip()
                takeoff_str = takeoff if (takeoff and takeoff.endswith("%")) else (f"{takeoff}%" if takeoff else "")
                if is_stock:
                    try:
                        sp = str(stock_raw or "").strip()
                        if not sp:
                            continue
                        sp_num = float(sp)
                        sp_str = f"{sp_num:.2f}"
                    except Exception:
                        sp_str = str(stock_raw).strip()
                    tps.append(f"${sp_str}{f' - {takeoff_str}' if takeoff_str else ''}")
                    continue

                per = str(data_copy.get(f"tp{i}_per") or "").strip()
                if not per:
                    continue
                per_str = per if per.endswith("%") else f"{per}%"
                price_raw = data_copy.get(f"tp{i}_price")
                try:
                    price_str = f"{float(price_raw):.3f}" if price_raw is not None and str(price_raw).strip() != "" else "0.000"
                except Exception:
                    price_str = "0.000"
                tps.append(f"{price_str}({per_str}){f' - {takeoff_str}' if takeoff_str else ''}")

            sl_per = str(data_copy.get("sl_per") or "").strip()
            sl_per_str = sl_per if (sl_per and sl_per.endswith("%")) else (f"{sl_per}%" if sl_per else "")
            sl_price_raw = data_copy.get("sl_price")
            try:
                sl_price_str = f"{float(sl_price_raw):.3f}" if sl_price_raw is not None and str(sl_price_raw).strip() != "" else "0.000"
            except Exception:
                sl_price_str = "0.000"

            # Show Trade Plan even if option price isn't computed yet (defaults to 0.000)
            if tps or sl_per_str or sl_price_str:
                # Insert after last option-related field if possible, else append.
                insert_at = len(fields)
                for idx, f in enumerate(fields):
                    n = str((f or {}).get("name") or "").lower()
                    v = str((f or {}).get("value") or "").lower()
                    if any(k in n for k in ("expiration", "strike", "option", "price")) or any(k in v for k in ("expiration", "strike", "option", "price")):
                        insert_at = idx + 1

                injected = []
                injected.append({"name": "", "value": "\u200b", "inline": False})
                injected.append({"name": "📝 **Trade Plan**", "value": "", "inline": False})
                if tps:
                    injected.append({"name": f"🎯 Targets: {', '.join(tps)}", "value": "", "inline": False})
                injected.append(
                    {
                        "name": f"🛑 Stop Loss: {sl_price_str}{f'({sl_per_str})' if sl_per_str else ''}",
                        "value": "",
                        "inline": False,
                    }
                )

                fields[insert_at:insert_at] = injected
    
    embed = {
        "color": color_int,
        "fields": fields,
        "footer": {
            "text": footer
        } if footer else None
    }
    
    # Add title only if it should be shown and is not empty
    if show_title and title:
        embed["title"] = title
    
    # Add description only if it should be shown and is not empty
    if show_description and description:
        embed["description"] = description
    
    # Remove footer if empty
    if not footer:
        del embed["footer"]
    
    return embed

def user_login(request):
    """User login view"""
    if request.user.is_authenticated:
        return redirect('dashboard')
    
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        
        user = authenticate(request, username=username, password=password)
        if user is not None:
            login(request, user)
            messages.success(request, f'Welcome back, {user.username}!')
            next_url = request.GET.get('next', None)
            if next_url:
                return redirect(next_url)
            return redirect('dashboard')
        else:
            messages.error(request, 'Invalid username or password.')
    
    return render(request, 'signals/login.html')

def user_logout(request):
    """User logout view"""
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    return redirect('user_login')

@login_required
def profile(request):
    """User profile view - allows users to update their own profile"""
    user = request.user
    
    # Get or create profile with default values
    try:
        profile = UserProfile.objects.get(user=user)
    except UserProfile.DoesNotExist:
        profile = UserProfile.objects.create(
            user=user,
            discord_channel_name='',
            discord_channel_webhook=''
        )
    
    password_form = PasswordChangeForm(user=user)
    email_value = user.email

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip() or 'update_email'

        if action == 'change_password':
            password_form = PasswordChangeForm(user=user, data=request.POST)
            if password_form.is_valid():
                updated_user = password_form.save()
                update_session_auth_hash(request, updated_user)  # keep user logged in
                messages.success(request, 'Your password has been changed successfully!')
                return redirect('profile')
            messages.error(request, 'Please correct the password errors below.')
        else:
            email_value = request.POST.get('email', '').strip()

            errors = []
            if not email_value:
                errors.append('Email is required.')

            if errors:
                for error in errors:
                    messages.error(request, error)
            else:
                user.email = email_value
                user.save()
                messages.success(request, 'Profile updated successfully!')
                return redirect('profile')

    # Stats for profile UI
    signals_created = Signal.objects.filter(user=user).count()
    active_channels_count = DiscordChannel.objects.filter(user=user, is_active=True).count()
    default_channel = (
        DiscordChannel.objects.filter(user=user, is_default=True).first()
        or DiscordChannel.objects.filter(user=user, is_active=True).first()
        or DiscordChannel.objects.filter(user=user).first()
    )

    return render(request, 'signals/profile.html', {
        'user': user,
        'profile': profile,
        'signals_created': signals_created,
        'active_channels_count': active_channels_count,
        'default_channel': default_channel,
        'password_form': password_form,
        'email_value': email_value,
    })

@login_required
def change_password(request):
    """Change password view"""
    # Password change is now handled inline on the Profile page.
    return redirect('profile')

@login_required
def dashboard(request):
    if request.method == 'POST':
        form = SignalForm(request.POST, user=request.user)
        if form.is_valid():
            # Get signal type and data from cleaned form data
            signal_type = form.cleaned_data['signal_type']
            signal_data = form.cleaned_data.get('data', {})
            
            # Ensure signal_data is a dict (it should be after form validation)
            if not isinstance(signal_data, dict):
                try:
                    signal_data = json.loads(signal_data) if signal_data else {}
                except (json.JSONDecodeError, TypeError):
                    signal_data = {}

            # Capture trade_type (global publish control) into signal_data for templates/history
            trade_type = (request.POST.get('trade_type') or '').strip()
            if trade_type and 'trade_type' not in signal_data:
                signal_data['trade_type'] = trade_type

            # Backend safety net: if option_price wasn't computed client-side, compute it here (best-effort)
            def _truthy(v) -> bool:
                if isinstance(v, bool):
                    return v
                s = str(v or '').strip().lower()
                return s in ('1', 'true', 'yes', 'y', 'on')

            def _is_zero_price(v) -> bool:
                try:
                    if v is None:
                        return True
                    s = str(v).strip()
                    if not s:
                        return True
                    return float(s) == 0.0
                except Exception:
                    return True

            try:
                is_shares = _truthy(signal_data.get('is_shares', False))
                if not is_shares:
                    sym = _normalize_symbol(signal_data.get('ticker') or signal_data.get('symbol') or '')
                    if sym and _is_zero_price(signal_data.get('option_price')):
                        polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
                        if polygon_key:
                            side_raw = str(signal_data.get('option_type') or 'CALL').strip().lower()
                            side = 'put' if 'put' in side_raw else 'call'
                            tt = str(signal_data.get('trade_type') or 'swing').strip().lower() or 'swing'

                            client = PolygonClient(polygon_key)
                            underlying_price = client.get_share_current_price(sym)
                            if underlying_price is not None:
                                # DTE windows per trade type (same as /api/best-option/)
                                import datetime as dt
                                today = dt.date.today()
                                if tt == "scalp":
                                    lo, hi = 1, 30
                                elif tt == "leap":
                                    lo, hi = 60, 90
                                else:
                                    lo, hi = 6, 45
                                exp_gte = (today + dt.timedelta(days=lo)).isoformat()
                                exp_lte = (today + dt.timedelta(days=hi)).isoformat()

                                snaps = client.get_option_chain_snapshots(
                                    underlying=sym,
                                    side=side,
                                    expiration_gte=exp_gte,
                                    expiration_lte=exp_lte,
                                    limit=250,
                                    max_pages=4,
                                    timeout=12,
                                ) or []

                                best = client.pick_best_option_from_snapshots(
                                    snapshots=snaps,
                                    underlying_price=float(underlying_price),
                                    trade_type=tt,
                                    side=side,
                                )
                                if best and best.get("option_price") is not None:
                                    # Fill key option fields used by templates
                                    signal_data["strike"] = best.get("strike") or signal_data.get("strike") or ""
                                    signal_data["expiration"] = best.get("expiration") or signal_data.get("expiration") or ""
                                    signal_data["option_contract"] = best.get("contract") or signal_data.get("option_contract") or ""
                                    opt_price = best.get("option_price")
                                    try:
                                        opt_price_f = float(opt_price)
                                    except Exception:
                                        opt_price_f = 0.0
                                    signal_data["option_price"] = f"{opt_price_f:.3f}"
                                    # Also populate common price fields for legacy templates
                                    if _is_zero_price(signal_data.get("price")):
                                        signal_data["price"] = f"{opt_price_f:.3f}"
                                    if _is_zero_price(signal_data.get("entry_price")):
                                        signal_data["entry_price"] = f"{opt_price_f:.3f}"

                                    # Compute TP/SL prices if missing (best-effort)
                                    def _get_per(key: str) -> float:
                                        try:
                                            v = str(signal_data.get(key) or '').strip().replace('%', '')
                                            return float(v) if v else 0.0
                                        except Exception:
                                            return 0.0

                                    for i in range(1, 7):
                                        mode = str(signal_data.get(f"tp{i}_mode") or "").strip().lower()
                                        if mode in ("stock", "stock_price", "underlying", "share_price"):
                                            continue
                                        per = _get_per(f"tp{i}_per")
                                        if per > 0:
                                            signal_data[f"tp{i}_price"] = f"{(opt_price_f * (1.0 + per / 100.0)):.3f}"
                                    sl_per = _get_per("sl_per")
                                    if sl_per > 0:
                                        signal_data["sl_price"] = f"{(opt_price_f * (1.0 - sl_per / 100.0)):.3f}"
            except Exception:
                # Never block submission due to option quote issues; dashboard preview may still show best-effort.
                pass
            
            # Get selected Discord channel if provided
            discord_channel_id = request.POST.get('discord_channel')
            discord_channel = None
            if discord_channel_id:
                try:
                    discord_channel = DiscordChannel.objects.get(id=discord_channel_id, user=request.user, is_active=True)
                except DiscordChannel.DoesNotExist:
                    messages.warning(request, 'Selected Discord channel not found. Using default channel.')
            
            # Prepare unsaved signal instance for validation
            signal_instance = Signal(
                user=request.user,
                signal_type=signal_type,
                data=signal_data,
                discord_channel=discord_channel
            )

            embed = get_signal_template(signal_instance)
            is_valid_embed, _, embed_error = validate_embed(embed)

            if not is_valid_embed:
                error_message = embed_error or 'Discord embed exceeds Discord limitations. Please shorten the content.'
                form.add_error(None, error_message)
                messages.error(request, error_message)
            else:
                # Save signal and send to Discord
                signal_instance.save()
                success = send_to_discord(signal_instance)
                
                if success:
                    messages.success(
                        request,
                        'Signal submitted and sent to Discord successfully!'
                    )
                else:
                    messages.warning(
                        request,
                        'Signal saved but failed to send to Discord. Please ensure your user profile has a Discord webhook configured.'
                    )
                
                return redirect('dashboard')
    else:
        form = SignalForm(user=request.user)
    
    # Get recent signals for display (only for current user)
    # recent_signals = Signal.objects.filter(user=request.user).select_related('signal_type', 'user').all()[:10]
    recent_signals = []
    
    # Get all signal types (system defaults + user's custom types) for JavaScript with serialized variables
    signal_types = SignalType.objects.filter(
        Q(user__isnull=True) | Q(user=request.user)
    )
    signal_types_data = []
    for st in signal_types:
        # JSONField values are already JSON-serializable (lists/dicts); pass them as native
        # objects and use Django's json_script in the template.
        signal_types_data.append({
            'id': st.id,
            'variables': st.variables or [],
            'title_template': st.title_template or '',
            'description_template': st.description_template or '',
            'footer_template': st.footer_template or '',
            'color': st.color or '#000000',
            'fields_template': st.fileds_template or [],
            'show_title_default': getattr(st, 'show_title_default', True),
            'show_description_default': getattr(st, 'show_description_default', True)
        })
    
    # Get user's Discord channels
    discord_channels = DiscordChannel.objects.filter(user=request.user, is_active=True).order_by('-is_default', 'channel_name')
    
    # Saved per-user Trade Plan presets (for dropdown)
    presets = []
    try:
        qs = UserTradePlanPreset.objects.filter(user=request.user).order_by("-is_default", "-updated_at", "name")
        for p in qs:
            presets.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "plan": p.plan if isinstance(p.plan, dict) else {},
                    "is_default": bool(p.is_default),
                }
            )
    except Exception:
        presets = []

    return render(request, 'signals/dashboard.html', {
        'form': form,
        'recent_signals': recent_signals,
        'signal_types_data': signal_types_data,
        'discord_channels': discord_channels,
        'trade_plan_presets': presets,
    })


@login_required
@require_http_methods(["GET", "POST"])
def trade_plan_api(request):
    """
    Persist per-user Trade Plan presets.

    GET  -> { plans: [{id,name,plan,is_default}, ...] }
    POST -> accepts JSON with an action:
            - {action:"create", name, plan, set_default?}
            - {action:"update", id, name?, plan}
            - {action:"delete", id}
            - {action:"set_default", id}

    Back-compat: POST without action updates/creates the user's default preset named "Default".
    """
    if request.method == "GET":
        plans = []
        for p in UserTradePlanPreset.objects.filter(user=request.user).order_by("-is_default", "-updated_at", "name"):
            plans.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "plan": p.plan if isinstance(p.plan, dict) else {},
                    "is_default": bool(p.is_default),
                }
            )
        return JsonResponse({"plans": plans})

    # POST
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    action = str(payload.get("action") or "").strip().lower()
    preset_id = payload.get("id")
    name = str(payload.get("name") or "").strip()
    plan = payload.get("plan")
    set_default = payload.get("set_default") is True

    def _clean_plan(plan_obj):
        plan_obj = plan_obj if isinstance(plan_obj, dict) else {}
        tp_levels = plan_obj.get("tp_levels")
        sl_per = plan_obj.get("sl_per")

        if tp_levels is None:
            tp_levels = []
        if not isinstance(tp_levels, list):
            raise ValueError("tp_levels must be a list")

        if len(tp_levels) > 6:
            tp_levels = tp_levels[:6]

        cleaned_levels = []
        for item in tp_levels:
            if not isinstance(item, dict):
                continue
            mode = str(item.get("mode") or "").strip().lower()
            if mode not in ("percent", "stock", "stock_price", "underlying", "share_price", ""):
                mode = ""
            per = str(item.get("per") or "").strip()
            stock_price = str(item.get("stock_price") or item.get("stockPrice") or "").strip()
            takeoff = str(item.get("takeoff") or "").strip()
            cleaned_levels.append({"mode": mode or "percent", "per": per, "stock_price": stock_price, "takeoff": takeoff})

        sl_per_str = str(sl_per or "").strip()
        return {"version": 1, "tp_levels": cleaned_levels, "sl_per": sl_per_str}

    # Back-compat path: previous frontend posted tp_levels/sl_per directly.
    if not action:
        plan = _clean_plan({"tp_levels": payload.get("tp_levels"), "sl_per": payload.get("sl_per")})
        obj = UserTradePlanPreset.objects.filter(user=request.user, name="Default").first()
        if not obj:
            obj = UserTradePlanPreset.objects.create(user=request.user, name="Default", plan=plan, is_default=True)
        else:
            obj.plan = plan
            obj.is_default = True
            obj.save(update_fields=["plan", "is_default", "updated_at"])
        return JsonResponse(
            {
                "ok": True,
                "preset": {"id": obj.id, "name": obj.name, "plan": obj.plan, "is_default": bool(obj.is_default)},
            }
        )

    if action == "create":
        if not name:
            return JsonResponse({"error": "name is required"}, status=400)
        try:
            cleaned = _clean_plan(plan)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        try:
            obj = UserTradePlanPreset.objects.create(user=request.user, name=name, plan=cleaned, is_default=set_default)
        except Exception:
            return JsonResponse({"error": "Could not create preset (name may already exist)"}, status=400)
        return JsonResponse(
            {"ok": True, "preset": {"id": obj.id, "name": obj.name, "plan": obj.plan, "is_default": bool(obj.is_default)}}
        )

    if action == "update":
        if not preset_id:
            return JsonResponse({"error": "id is required"}, status=400)
        obj = UserTradePlanPreset.objects.filter(user=request.user, id=preset_id).first()
        if not obj:
            return JsonResponse({"error": "Preset not found"}, status=404)
        try:
            cleaned = _clean_plan(plan)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        if name and name != obj.name:
            # Best-effort rename (unique per user)
            if UserTradePlanPreset.objects.filter(user=request.user, name=name).exclude(id=obj.id).exists():
                return JsonResponse({"error": "A preset with this name already exists"}, status=400)
            obj.name = name
        obj.plan = cleaned
        if set_default:
            obj.is_default = True
        obj.save()
        return JsonResponse(
            {"ok": True, "preset": {"id": obj.id, "name": obj.name, "plan": obj.plan, "is_default": bool(obj.is_default)}}
        )

    if action == "set_default":
        if not preset_id:
            return JsonResponse({"error": "id is required"}, status=400)
        obj = UserTradePlanPreset.objects.filter(user=request.user, id=preset_id).first()
        if not obj:
            return JsonResponse({"error": "Preset not found"}, status=404)
        obj.is_default = True
        obj.save(update_fields=["is_default", "updated_at"])
        return JsonResponse({"ok": True})

    if action == "delete":
        if not preset_id:
            return JsonResponse({"error": "id is required"}, status=400)
        obj = UserTradePlanPreset.objects.filter(user=request.user, id=preset_id).first()
        if not obj:
            return JsonResponse({"error": "Preset not found"}, status=404)
        was_default = bool(obj.is_default)
        obj.delete()
        if was_default:
            # Promote newest preset to default (if any remain)
            nxt = UserTradePlanPreset.objects.filter(user=request.user).order_by("-updated_at").first()
            if nxt:
                nxt.is_default = True
                nxt.save(update_fields=["is_default", "updated_at"])
        return JsonResponse({"ok": True})

    return JsonResponse({"error": "Invalid action"}, status=400)

@login_required
def signals_history(request):
    """View all submitted signals for current user"""
    signals = Signal.objects.filter(user=request.user).select_related('signal_type', 'user').all().order_by('-created_at')
    
    # Filter by signal type if provided
    signal_type = request.GET.get('type')
    if signal_type:
        signals = signals.filter(signal_type__name=signal_type)
    
    # Paginate signals
    paginator = Paginator(signals, 25)  # Show 25 signals per page
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    return render(request, 'signals/history.html', {
        'signals': page_obj,
        'filter_type': signal_type,
        'page_obj': page_obj
    })

@login_required
def get_signal_type_variables(request):
    """API endpoint to get variables for a specific signal type"""
    signal_type_id = request.GET.get('signal_type_id')
    if not signal_type_id:
        return JsonResponse({'error': 'signal_type_id is required'}, status=400)
    
    try:
        signal_type = SignalType.objects.get(id=signal_type_id)
        # Check if user has access to this signal type
        if signal_type.user and signal_type.user != request.user:
            return JsonResponse({'error': 'Access denied'}, status=403)
        
        return JsonResponse({
            'variables': signal_type.variables or []
        })
    except SignalType.DoesNotExist:
        return JsonResponse({'error': 'Signal type not found'}, status=404)


@login_required
@require_GET
def us_tickers(request):
    """
    API endpoint for a cached list of US ticker symbols used by ticker dropdowns.

    Query params:
      - source: "tradingview" (live) or "cache" (us_tickers.json); default: tradingview when q is provided, else cache
      - q: optional search string (matches symbol substring or name substring)
      - limit: optional max results (capped)
    """
    source = (request.GET.get("source") or "").strip().lower()
    q = (request.GET.get("q") or "").strip()
    limit_raw = (request.GET.get("limit") or "").strip()

    limit = 0
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            limit = 0
    # Hard cap to avoid accidentally returning huge payloads when used for search UX.
    if limit < 0:
        limit = 0
    if limit > 200:
        limit = 200

    # Default behavior:
    # - For live search UX (q provided): TradingView (unless explicitly overridden)
    # - For full list load (no q): cached file (fast, avoids huge remote calls)
    if not source:
        source = "tradingview" if q else "cache"

    include_etfs = True  # keep consistent with existing behavior unless you want a flag later

    if source == "tradingview":
        # Live search only (TradingView endpoint is search-oriented, not a full-universe dump).
        try:
            tickers = _search_tickers_tradingview(q, limit=limit or 40, include_etfs=include_etfs) if q else []
            return JsonResponse({"tickers": tickers, "source": "tradingview"})
        except Exception as e:
            # Fall back to cache to keep UI usable.
            # (We intentionally don't expose internal error details to the client.)
            tickers = []
            # Continue into cache flow below.

    # Cache flow (fallback or explicit)
    tickers_all = list(get_us_tickers() or [])
    tickers = tickers_all

    if q:
        q_upper = q.upper()
        q_lower = q.lower()
        filtered = []
        for t in tickers_all:
            if not isinstance(t, dict):
                continue
            sym = str(t.get("symbol") or "").strip().upper()
            name = str(t.get("name") or "").strip()
            if not sym:
                continue
            if q_upper in sym or (name and q_lower in name.lower()):
                filtered.append({"symbol": sym, "name": name})
        filtered.sort(key=lambda r: (0 if r["symbol"].startswith(q_upper) else 1, r["symbol"]))
        tickers = filtered

    if limit:
        tickers = tickers[:limit]

    return JsonResponse({"tickers": tickers, "source": "cache"})


@login_required
@require_GET
def quote(request):
    """
    Quote endpoint used by the dashboard to auto-fill the current price.

    Query params:
      - symbol: ticker symbol (e.g. AAPL)
    """
    
    symbol = _normalize_symbol(request.GET.get("symbol") or "")
    
    if not symbol:
        logger.error(f"Symbol is required")
        return JsonResponse({"error": "symbol is required"}, status=400)
    
    logger.info(f"Quote request for symbol: {symbol}")

    # Polygon-only
    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    if not polygon_key:
        return JsonResponse(
            {"error": "POLYGON_API_KEY is not set", "source": "polygon"},
            status=502,
        )

    client = PolygonClient(polygon_key)
    q = client.get_latest_quote(symbol)
    if q and q.get("p") is not None:
        company_name = ""
        try:
            company_name = client.get_company_name(symbol) or ""
        except Exception:
            company_name = ""
        return JsonResponse(
            {
                "symbol": symbol,
                "price": round(float(q.get("p")), 3),
                "bid": (round(float(q.get("bid")), 3) if q.get("bid") is not None else None),
                "ask": (round(float(q.get("ask")), 3) if q.get("ask") is not None else None),
                "company_name": company_name,
                "source": "polygon",
            }
        )

    # Polygon configured but no quote returned
    payload = {"error": "quote fetch failed", "source": "polygon", "symbol": symbol}
    if getattr(settings, "DEBUG", False):
        payload["polygon_error"] = getattr(client, "last_error", None)
    return JsonResponse(payload, status=502)


@login_required
@require_GET
def option_suggest(request):
    """
    Suggest an at-the-money (ATM) option (nearest expiration) and return its price.

    Query params:
      - symbol: ticker symbol (e.g. AAPL)
      - side: "call" or "put"
    """
    symbol = _normalize_symbol(request.GET.get("symbol") or "")
    side = (request.GET.get("side") or "").strip().lower()
    if side not in ("call", "put"):
        return JsonResponse({"error": "side must be call or put"}, status=400)
    if not symbol:
        return JsonResponse({"error": "symbol is required"}, status=400)

    # Fetch underlying price first (prefer Polygon if configured; fallback Yahoo)
    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    quote_url = "https://query2.finance.yahoo.com/v7/finance/quote"
    opt_url = f"https://query2.finance.yahoo.com/v7/finance/options/{symbol}"

    try:
        underlying_price = None
        if polygon_key:
            client = PolygonClient(polygon_key)
            underlying_price = client.get_share_current_price(symbol)

        if underlying_price is None:
            qresp = requests.get(quote_url, params={"symbols": symbol}, timeout=6)
            qresp.raise_for_status()
            qpayload = qresp.json() if qresp.content else {}
            qresult = ((qpayload.get("quoteResponse") or {}).get("result") or [])
            if not qresult:
                return JsonResponse({"error": "symbol not found"}, status=404)
            underlying = qresult[0] or {}
            underlying_price = underlying.get("regularMarketPrice")
            if underlying_price is None:
                return JsonResponse({"error": "underlying price unavailable"}, status=502)

        oresp = requests.get(opt_url, timeout=8)
        oresp.raise_for_status()
        opayload = oresp.json() if oresp.content else {}
        chain = ((opayload.get("optionChain") or {}).get("result") or [])
        if not chain:
            return JsonResponse({"error": "options unavailable"}, status=404)

        chain0 = chain[0] or {}
        expirations = chain0.get("expirationDates") or []
        options = chain0.get("options") or []
        if not expirations or not options:
            return JsonResponse({"error": "options unavailable"}, status=404)

        # Yahoo returns an options list matching a particular expiration (usually first in response).
        opt0 = options[0] or {}
        contracts = opt0.get("calls" if side == "call" else "puts") or []
        if not contracts:
            return JsonResponse({"error": "no contracts"}, status=404)

        # Pick strike closest to underlying price.
        best = None
        best_dist = None
        for c in contracts:
            strike = c.get("strike")
            if strike is None:
                continue
            dist = abs(float(strike) - float(underlying_price))
            if best is None or dist < best_dist:
                best = c
                best_dist = dist

        if not best:
            return JsonResponse({"error": "no contracts"}, status=404)

        bid = best.get("bid")
        ask = best.get("ask")
        last_price = best.get("lastPrice")
        mid = None
        try:
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                mid = (float(bid) + float(ask)) / 2.0
        except Exception:
            mid = None

        option_price = mid if mid is not None else last_price

        # expiration in the response is seconds since epoch; convert to YYYY-MM-DD in UTC.
        exp_epoch = best.get("expiration")
        exp_date = ""
        if exp_epoch:
            try:
                exp_date = datetime.utcfromtimestamp(int(exp_epoch)).strftime("%Y-%m-%d")
            except Exception:
                exp_date = ""

        return JsonResponse(
            {
                "symbol": symbol,
                "underlying_price": underlying_price,
                "side": side,
                "contract": best.get("contractSymbol") or "",
                "strike": best.get("strike"),
                "expiration": exp_date,
                "option_price": option_price,
                "bid": bid,
                "ask": ask,
                "source": "yahoo",
            }
        )
    except requests.RequestException:
        return JsonResponse({"error": "option fetch failed"}, status=502)


@login_required
@require_GET
def option_quote(request):
    """
    Quote a specific option contract from Polygon based on:
      - symbol (AAPL)
      - expiration (YYYY-MM-DD)
      - strike (float)
      - side (call/put or CALL/PUT)
    """
    symbol = _normalize_symbol(request.GET.get("symbol") or "")
    expiration = (request.GET.get("expiration") or "").strip()
    strike_raw = (request.GET.get("strike") or "").strip()
    side_raw = (request.GET.get("side") or "").strip().lower()

    if not symbol:
        return JsonResponse({"error": "symbol is required"}, status=400)
    if not expiration:
        return JsonResponse({"error": "expiration is required"}, status=400)
    if not strike_raw:
        return JsonResponse({"error": "strike is required"}, status=400)

    side = "call" if "call" in side_raw or side_raw == "c" else ("put" if "put" in side_raw or side_raw == "p" else "")
    if side not in ("call", "put"):
        return JsonResponse({"error": "side must be call or put"}, status=400)

    try:
        strike = float(strike_raw)
    except ValueError:
        return JsonResponse({"error": "invalid strike"}, status=400)

    # Parse YYYY-MM-DD -> YYMMDD
    try:
        exp_dt = datetime.strptime(expiration, "%Y-%m-%d")
    except ValueError:
        return JsonResponse({"error": "expiration must be YYYY-MM-DD"}, status=400)

    exp_yyMMdd = exp_dt.strftime("%y%m%d")
    strike_formatted = f"{int(round(strike * 1000)):08d}"
    cp = "C" if side == "call" else "P"
    contract = f"O:{symbol}{exp_yyMMdd}{cp}{strike_formatted}"

    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    if not polygon_key:
        return JsonResponse({"error": "POLYGON_API_KEY is not set", "source": "polygon"}, status=502)

    client = PolygonClient(polygon_key)
    q = client.get_option_quote(contract)
    if q and q.get("price") is not None:
        return JsonResponse(
            {
                "symbol": symbol,
                "expiration": expiration,
                "strike": strike,
                "side": side,
                "contract": contract,
                "price": round(float(q.get("price")), 3),
                "bid": (round(float(q.get("bid")), 3) if q.get("bid") is not None else None),
                "ask": (round(float(q.get("ask")), 3) if q.get("ask") is not None else None),
                "mid": (round(float(q.get("mid")), 3) if q.get("mid") is not None else None),
                "timestamp": q.get("timestamp"),
                "source": "polygon",
            }
        )

    # If the exact strike isn't a listed contract (common when strike defaults to stock price),
    # snap to the nearest listed strike and quote that contract instead.
    nearest = client.find_nearest_option_contract(underlying=symbol, expiration=expiration, side=side, target_strike=strike)
    if nearest and nearest.get("contract"):
        resolved_contract = nearest["contract"]
        resolved_strike = nearest.get("strike")
        q2 = client.get_option_quote(resolved_contract)
        if q2 and q2.get("price") is not None:
            return JsonResponse(
                {
                    "symbol": symbol,
                    "expiration": expiration,
                    "side": side,
                    "requested_strike": strike,
                    "requested_contract": contract,
                    "strike": resolved_strike,
                    "contract": resolved_contract,
                    "price": round(float(q2.get("price")), 3),
                    "bid": (round(float(q2.get("bid")), 3) if q2.get("bid") is not None else None),
                    "ask": (round(float(q2.get("ask")), 3) if q2.get("ask") is not None else None),
                    "mid": (round(float(q2.get("mid")), 3) if q2.get("mid") is not None else None),
                    "timestamp": q2.get("timestamp"),
                    "source": "polygon",
                    "resolved": True,
                }
            )

    payload = {"error": "option quote failed", "source": "polygon", "contract": contract}
    if getattr(settings, "DEBUG", False):
        payload["polygon_error"] = getattr(client, "last_error", None)
    return JsonResponse(payload, status=502)


@login_required
@require_GET
def best_option(request):
    """
    Pick the best option contract for an underlying according to trade_type rules.

    Query params:
      - symbol: underlying ticker (e.g. AAPL)
      - trade_type: Scalp|Swing|Leap
      - side: call|put (optional; defaults to call)

    Returns:
      { contract, strike, expiration, side, option_price, bid, ask, spread, delta, open_interest, dte, underlying_price }
    """
    symbol = _normalize_symbol(request.GET.get("symbol") or "")
    trade_type = (request.GET.get("trade_type") or "").strip().lower() or "swing"
    side = (request.GET.get("side") or "").strip().lower() or "call"
    if side not in ("call", "put"):
        return JsonResponse({"error": "side must be call or put"}, status=400)
    if not symbol:
        return JsonResponse({"error": "symbol is required"}, status=400)

    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    if not polygon_key:
        return JsonResponse({"error": "POLYGON_API_KEY is not set", "source": "polygon"}, status=502)

    import datetime as dt

    client = PolygonClient(polygon_key)
    underlying_price = client.get_share_current_price(symbol)
    if underlying_price is None:
        payload = {"error": "underlying price unavailable", "source": "polygon"}
        if getattr(settings, "DEBUG", False):
            payload["polygon_error"] = getattr(client, "last_error", None)
        return JsonResponse(payload, status=502)

    today = dt.date.today()
    # DTE windows per trade type
    if trade_type == "scalp":
        lo, hi = 1, 30
    elif trade_type == "leap":
        lo, hi = 60, 90
    else:  # swing
        lo, hi = 6, 45

    exp_gte = (today + dt.timedelta(days=lo)).isoformat()
    exp_lte = (today + dt.timedelta(days=hi)).isoformat()

    snaps = client.get_option_chain_snapshots(
        underlying=symbol,
        side=side,
        expiration_gte=exp_gte,
        expiration_lte=exp_lte,
        limit=250,
        max_pages=4,
        timeout=12,
    )
    if snaps is None:
        payload = {"error": "options unavailable", "source": "polygon"}
        if getattr(settings, "DEBUG", False):
            payload["polygon_error"] = getattr(client, "last_error", None)
            payload["query"] = {"expiration_gte": exp_gte, "expiration_lte": exp_lte, "side": side, "trade_type": trade_type}
        return JsonResponse(payload, status=502)

    best = client.pick_best_option_from_snapshots(
        snapshots=snaps,
        underlying_price=float(underlying_price),
        trade_type=trade_type,
        side=side,
    )
    if not best:
        return JsonResponse(
            {
                "error": "No suitable option contract found",
                "source": "polygon",
                "symbol": symbol,
                "underlying_price": underlying_price,
                "side": side,
                "trade_type": trade_type,
            },
            status=404,
        )

    best["symbol"] = symbol
    best["side"] = side
    best["trade_type"] = trade_type
    try:
        best["underlying_price"] = round(float(underlying_price), 3)
    except Exception:
        best["underlying_price"] = underlying_price
    for k in ("option_price", "bid", "ask", "spread"):
        if k in best and best.get(k) is not None:
            try:
                best[k] = round(float(best.get(k)), 3)
            except Exception:
                pass
    best["source"] = "polygon_snapshot"
    return JsonResponse(best)

def is_superuser(user):
    """Check if user is superuser"""
    return user.is_superuser

@login_required
@user_passes_test(is_superuser)
def user_management(request):
    """List all users"""
    users = User.objects.all().prefetch_related('discord_channels').order_by('-date_joined')
    
    # Handle search
    search_query = request.GET.get('search', '').strip()
    if search_query:
        users = users.filter(
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(discord_channels__channel_name__icontains=search_query)
        ).distinct()

    # Role filter (for UI dropdown)
    role_filter = request.GET.get('role', 'all').strip().lower() or 'all'
    # Admin role is no longer exposed in the UI; treat it as "all" for old links.
    if role_filter == 'admin':
        role_filter = 'all'
    if role_filter == 'superuser':
        users = users.filter(is_superuser=True)
    elif role_filter == 'user':
        users = users.filter(is_staff=False, is_superuser=False)
    
    # Pagination
    paginator = Paginator(users, 25)  # Show 25 users per page
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    return render(request, 'signals/user_management.html', {
        'users': page_obj,
        'page_obj': page_obj,
        'search_query': search_query,
        'role_filter': role_filter,
    })

@login_required
@user_passes_test(is_superuser)
def user_create(request):
    """Create a new user"""
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '').strip()
        is_superuser_check = request.POST.get('is_superuser') == 'on'
        # Default to active if not specified (for create form)
        is_active = request.POST.get('is_active', 'on') == 'on'
        
        # Collect all Discord channels from POST data
        channels = []
        index = 0
        while True:
            channel_name = request.POST.get(f'channel_name_{index}', '').strip()
            webhook_url = request.POST.get(f'webhook_url_{index}', '').strip()
            is_default = request.POST.get(f'is_default_{index}') == 'on'
            channel_is_active = request.POST.get(f'is_active_{index}', 'on') == 'on'
            
            # If we have at least one field, consider it a channel attempt
            if channel_name or webhook_url:
                channels.append({
                    'name': channel_name,
                    'url': webhook_url,
                    'is_default': is_default,
                    'is_active': channel_is_active,
                    'index': index
                })
                index += 1
            else:
                break
        
        # Validate required fields
        errors = []
        if not username:
            errors.append('Username is required.')
        if not email:
            errors.append('Email is required.')
        if not password:
            errors.append('Password is required.')
        
        # Validate Discord channels - require at least one complete channel
        valid_channels = []
        for idx, channel in enumerate(channels):
            if channel['name'] and channel['url']:
                valid_channels.append(channel)
            elif channel['name'] or channel['url']:
                errors.append(f'Channel {idx + 1}: Both channel name and webhook URL are required.')
        
        if not valid_channels:
            errors.append('At least one Discord channel with both channel name and webhook URL is required.')
        
        # Ensure exactly one default channel when valid channels exist
        if valid_channels:
            if not any(c.get('is_default') for c in valid_channels):
                valid_channels[0]['is_default'] = True
            else:
                seen_default = False
                for c in valid_channels:
                    if c.get('is_default') and not seen_default:
                        seen_default = True
                    elif c.get('is_default') and seen_default:
                        c['is_default'] = False

        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'signals/user_form.html', {
                'form_type': 'create',
                'username': username,
                'email': email,
                'is_superuser': is_superuser_check,
                'is_active': is_active,
                'channels_data': valid_channels or channels,
            })
        
        # Check if username already exists
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
            return render(request, 'signals/user_form.html', {
                'form_type': 'create',
                'username': username,
                'email': email,
                'is_superuser': is_superuser_check,
                'is_active': is_active,
                'channels_data': valid_channels or channels,
            })
        
        # Create user
        user = User.objects.create_user(
            username=username,
            email=email,
            password=password,
            is_superuser=is_superuser_check,
            is_staff=is_superuser_check,
            is_active=is_active
        )
        
        # Create user profile (empty)
        UserProfile.objects.create(
            user=user,
            discord_channel_name='',
            discord_channel_webhook=''
        )
        
        # Create Discord channels
        channels_created = 0
        channels_with_errors = []
        
        for channel in valid_channels:
            try:
                DiscordChannel.objects.create(
                    user=user,
                    channel_name=channel['name'],
                    webhook_url=channel['url'],
                    is_default=channel['is_default'],
                    is_active=channel.get('is_active', True),
                )
                channels_created += 1
            except Exception as e:
                channels_with_errors.append(f"{channel['name']}: {str(e)}")
        
        # Show success/error messages
        if channels_created > 0:
            if channels_created == 1:
                messages.success(request, f'User "{username}" and Discord channel created successfully!')
            else:
                messages.success(request, f'User "{username}" and {channels_created} Discord channels created successfully!')
        
        if channels_with_errors:
            for error in channels_with_errors:
                messages.warning(request, f'Error creating channel: {error}')
        
        return redirect('user_management')
    
    return render(request, 'signals/user_form.html', {
        'form_type': 'create'
    })

@login_required
@user_passes_test(is_superuser)
def user_edit(request, user_id):
    """Edit an existing user"""
    managed_user = get_object_or_404(User, id=user_id)
    
    # Prevent editing superuser by non-superusers (extra safety)
    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to edit users.')
        return redirect('user_management')
    
    # Handle legacy Discord channel management (older edit UI)
    if request.method == 'POST' and 'action' in request.POST:
        action = request.POST.get('action')
        
        if action == 'add_channel':
            channel_name = request.POST.get('channel_name', '').strip()
            webhook_url = request.POST.get('webhook_url', '').strip()
            is_default = request.POST.get('is_default') == 'on'
            
            if not channel_name or not webhook_url:
                messages.error(request, 'Channel name and webhook URL are required.')
            else:
                try:
                    channel = DiscordChannel.objects.create(
                        user=managed_user,
                        channel_name=channel_name,
                        webhook_url=webhook_url,
                        is_default=is_default
                    )
                    messages.success(request, f'Discord channel "{channel_name}" added successfully!')
                except Exception as e:
                    messages.error(request, f'Error adding channel: {str(e)}')
            return redirect('user_edit', user_id=managed_user.id)
        
        elif action == 'update_channel':
            channel_id = request.POST.get('channel_id')
            channel_name = request.POST.get('channel_name', '').strip()
            webhook_url = request.POST.get('webhook_url', '').strip()
            is_default = request.POST.get('is_default') == 'on'
            is_active = request.POST.get('is_active') == 'on'
            
            try:
                channel = DiscordChannel.objects.get(id=channel_id, user=managed_user)
                channel.channel_name = channel_name
                channel.webhook_url = webhook_url
                channel.is_default = is_default
                channel.is_active = is_active
                channel.save()
                messages.success(request, f'Discord channel "{channel_name}" updated successfully!')
            except DiscordChannel.DoesNotExist:
                messages.error(request, 'Channel not found.')
            except Exception as e:
                messages.error(request, f'Error updating channel: {str(e)}')
            return redirect('user_edit', user_id=managed_user.id)
        
        elif action == 'delete_channel':
            channel_id = request.POST.get('channel_id')
            try:
                channel = DiscordChannel.objects.get(id=channel_id, user=managed_user)
                channel_name = channel.channel_name
                channel.delete()
                messages.success(request, f'Discord channel "{channel_name}" deleted successfully!')
            except DiscordChannel.DoesNotExist:
                messages.error(request, 'Channel not found.')
            return redirect('user_edit', user_id=managed_user.id)
    
    # Handle user update
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        is_superuser_check = request.POST.get('is_superuser') == 'on'
        is_active = request.POST.get('is_active') == 'on'
        new_password = request.POST.get('password', '').strip()

        # Collect Discord channels from POST data (same format as create)
        channels = []
        index = 0
        while True:
            channel_id = request.POST.get(f'channel_id_{index}', '').strip()
            channel_name = request.POST.get(f'channel_name_{index}', '').strip()
            webhook_url = request.POST.get(f'webhook_url_{index}', '').strip()
            is_default = request.POST.get(f'is_default_{index}') == 'on'
            channel_is_active = request.POST.get(f'is_active_{index}', 'on') == 'on'

            if channel_id or channel_name or webhook_url:
                channels.append({
                    'id': channel_id or None,
                    'name': channel_name,
                    'url': webhook_url,
                    'is_default': is_default,
                    'is_active': channel_is_active,
                    'index': index
                })
                index += 1
            else:
                break
        
        # Validate required fields
        errors = []
        if not username:
            errors.append('Username is required.')
        if not email:
            errors.append('Email is required.')

        # Validate Discord channels - require at least one complete channel
        valid_channels = []
        for idx, channel in enumerate(channels):
            if channel['name'] and channel['url']:
                valid_channels.append(channel)
            elif channel['name'] or channel['url']:
                errors.append(f'Channel {idx + 1}: Both channel name and webhook URL are required.')

        if not valid_channels:
            errors.append('At least one Discord channel with both channel name and webhook URL is required.')

        # Ensure exactly one default channel when valid channels exist
        if valid_channels:
            if not any(c.get('is_default') for c in valid_channels):
                valid_channels[0]['is_default'] = True
            else:
                seen_default = False
                for c in valid_channels:
                    if c.get('is_default') and not seen_default:
                        seen_default = True
                    elif c.get('is_default') and seen_default:
                        c['is_default'] = False
        
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'signals/user_form.html', {
                'form_type': 'edit',
                'managed_user': managed_user,
                'username': username,
                'email': email,
                'is_superuser': is_superuser_check,
                'is_active': is_active,
                'channels_data': valid_channels or channels,
            })
        
        # Check if username already exists (for other users)
        if username != managed_user.username and User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
            return render(request, 'signals/user_form.html', {
                'form_type': 'edit',
                'managed_user': managed_user,
                'username': username,
                'email': email,
                'is_superuser': is_superuser_check,
                'is_active': is_active,
                'channels_data': valid_channels or channels,
            })
        
        # Update user
        managed_user.username = username
        managed_user.email = email
        managed_user.is_superuser = is_superuser_check
        managed_user.is_staff = is_superuser_check  # Staff status follows superuser status
        managed_user.is_active = is_active
        
        # Update password if provided
        if new_password:
            managed_user.set_password(new_password)
        
        managed_user.save()

        # Sync Discord channels
        existing = {str(c.id): c for c in DiscordChannel.objects.filter(user=managed_user)}
        kept_ids = set()

        for ch in valid_channels:
            ch_id = (ch.get('id') or '').strip()
            if ch_id and ch_id in existing:
                obj = existing[ch_id]
                obj.channel_name = ch['name']
                obj.webhook_url = ch['url']
                obj.is_default = bool(ch.get('is_default'))
                obj.is_active = bool(ch.get('is_active', True))
                obj.save()
                kept_ids.add(ch_id)
            else:
                obj = DiscordChannel.objects.create(
                    user=managed_user,
                    channel_name=ch['name'],
                    webhook_url=ch['url'],
                    is_default=bool(ch.get('is_default')),
                    is_active=bool(ch.get('is_active', True)),
                )
                kept_ids.add(str(obj.id))

        # Delete removed channels
        for ch_id, obj in existing.items():
            if ch_id not in kept_ids:
                obj.delete()
        
        messages.success(request, f'User "{username}" updated successfully!')
        return redirect('user_management')
    
    # Prefill existing channels for the unified edit form UI
    discord_channels = DiscordChannel.objects.filter(user=managed_user).order_by('-is_default', 'channel_name')
    channels_data = [
        {
            'id': c.id,
            'name': c.channel_name,
            'url': c.webhook_url,
            'is_default': c.is_default,
            'is_active': c.is_active,
        }
        for c in discord_channels
    ]
    
    return render(request, 'signals/user_form.html', {
        'form_type': 'edit',
        'managed_user': managed_user,
        'channels_data': channels_data,
    })

@login_required
@user_passes_test(is_superuser)
def user_delete(request, user_id):
    """Delete a user"""
    user = get_object_or_404(User, id=user_id)
    
    # Prevent users from deleting themselves
    if user.id == request.user.id:
        messages.error(request, 'You cannot delete your own account.')
        return redirect('user_management')
    
    # Delete user directly (JavaScript confirmation already handled in template)
    username = user.username
    user.delete()
    messages.success(request, f'User "{username}" deleted successfully!')
    return redirect('user_management')

@login_required
def signal_types_list(request):
    """List all signal types for the current user (system defaults + user's custom types)"""
    user_signal_types = SignalType.objects.filter(user=request.user).order_by('-created_at')
    system_signal_types = SignalType.objects.filter(user__isnull=True).order_by('name')
    
    return render(request, 'signals/signal_types_list.html', {
        'user_signal_types': user_signal_types,
        'system_signal_types': system_signal_types,
        'is_superuser': request.user.is_superuser,
    })

@login_required
def signal_type_create(request):
    """Create a new signal type"""
    can_create_system = request.user.is_superuser
    if request.method == 'POST':
        make_system = can_create_system and (request.POST.get('is_system') in ('1', 'true', 'on', 'yes'))
        owner = None if make_system else request.user
        form = SignalTypeForm(request.POST, user=owner)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = owner
            obj.save()
            messages.success(request, 'Signal type created successfully!')
            return redirect('signal_types_list')
    else:
        form = SignalTypeForm(user=request.user)
    
    return render(request, 'signals/signal_type_form.html', {
        'form': form,
        'form_type': 'create',
        'is_default': False,
        'is_locked': False,
        'can_edit_default': can_create_system,
        'is_system_checked': False,
    })

@login_required
def signal_type_edit(request, signal_type_id):
    """Edit an existing signal type"""
    signal_type = get_object_or_404(SignalType, id=signal_type_id)
    
    # Check if this is a system default template (user is None)
    is_default = signal_type.user is None
    can_edit_default = request.user.is_superuser
    is_locked = is_default and not can_edit_default
    
    # Check if user has permission to edit this signal type
    if (not request.user.is_superuser) and signal_type.user and signal_type.user != request.user:
        messages.error(request, 'You do not have permission to edit this signal type.')
        return redirect('signal_types_list')
    
    # Prevent editing default templates
    if is_locked and request.method == 'POST':
        messages.warning(request, 'System default templates are read-only and cannot be modified.')
        return redirect('signal_types_list')
    
    if request.method == 'POST':
        # Superusers may save as system template (user=NULL); others always save as their own.
        make_system = can_edit_default and (request.POST.get('is_system') in ('1', 'true', 'on', 'yes'))
        owner = None if make_system else (signal_type.user or request.user)
        if not can_edit_default:
            owner = request.user

        form = SignalTypeForm(request.POST, instance=signal_type, user=owner)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.user = owner
            obj.save()
            messages.success(request, 'Signal type updated successfully!')
            return redirect('signal_types_list')
    else:
        # Use the template owner for uniqueness validation
        form = SignalTypeForm(instance=signal_type, user=signal_type.user)
    
    return render(request, 'signals/signal_type_form.html', {
        'form': form,
        'form_type': 'edit',
        'is_default': is_default,
        'is_locked': is_locked,
        'can_edit_default': can_edit_default,
        'is_system_checked': is_default,
        'signal_type': signal_type
    })

@login_required
def signal_type_delete(request, signal_type_id):
    """Delete a signal type"""
    signal_type = get_object_or_404(SignalType, id=signal_type_id)
    is_default = signal_type.user is None
    
    # Check if user has permission to delete this signal type
    if is_default and (not request.user.is_superuser):
        messages.error(request, 'You do not have permission to delete system default templates.')
        return redirect('signal_types_list')
    if (not request.user.is_superuser) and signal_type.user and signal_type.user != request.user:
        messages.error(request, 'You do not have permission to delete this signal type.')
        return redirect('signal_types_list')
    
    if request.method == 'POST':
        name = signal_type.name
        signal_type.delete()
        messages.success(request, f'Signal type "{name}" deleted successfully!')
        return redirect('signal_types_list')
    
    return render(request, 'signals/signal_type_confirm_delete.html', {
        'signal_type': signal_type
    })

