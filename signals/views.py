from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
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
from decimal import Decimal, InvalidOperation
import logging
import requests
import re
import json
import html
from .forms import SignalForm, SignalTypeForm
from .models import Signal, SignalType, UserProfile, DiscordChannel, UserTradePlan, UserTradePlanPreset, Agreement, AgreementAcceptance, Position
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
    # Include "fund" so ETFs like SPY, QQQ come up (TradingView sometimes uses type "fund" for ETFs).
    allowed_types = {"stock"}
    if include_etfs:
        allowed_types.add("etf")
        allowed_types.add("fund")

    # Send uppercase so lowercase user input (e.g. "aapl") still finds symbols (e.g. AAPL)
    params = {
        "text": q.upper(),
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


def _search_crypto_tickers_polygon(q: str, *, limit: int) -> list[dict]:
    """
    Search crypto tickers via Polygon API.
    Returns list[{symbol, name}] for crypto symbols matching the query.
    """
    q = (q or "").strip().upper()
    if not q:
        return []
    
    polygon_key = getattr(settings, "POLYGON_API_KEY", "") or ""
    if not polygon_key:
        return []
    
    try:
        client = PolygonClient(polygon_key)
        
        # Polygon crypto tickers endpoint: /v3/reference/tickers
        # Search for crypto tickers matching the query
        params = {
            "market": "crypto",
            "search": q,
            "active": "true",
            "limit": min(limit or 50, 100),  # Polygon limit is 100
            "order": "ticker",
        }
        
        # Use PolygonClient's _get method
        data = client._get("/v3/reference/tickers", params=params, timeout=6)
        if not data or data.get("status") != "OK":
            return []
        
        results = data.get("results") or []
        crypto_tickers = []
        
        # Common crypto symbols to prioritize
        common_cryptos = {"BTC", "ETH", "SOL", "ADA", "DOT", "MATIC", "AVAX", "LINK", "UNI", "ATOM", 
                         "ALGO", "XRP", "DOGE", "SHIB", "LTC", "BCH", "ETC", "XLM", "AAVE", "SAND",
                         "MANA", "AXS", "ENJ", "CHZ", "FLOW", "NEAR", "FTM", "ICP", "APT", "ARB",
                         "OP", "SUI", "SEI", "TIA", "INJ", "RUNE", "THETA", "FIL", "EOS", "TRX"}
        
        seen = set()
        prioritized = []
        others = []
        
        for item in results:
            if not isinstance(item, dict):
                continue
            
            ticker_raw = str(item.get("ticker") or "").strip().upper()
            if not ticker_raw:
                continue
            
            # Only include USD pairs (exclude USDT and other pairs)
            # Skip if it ends with USDT or doesn't end with USD
            if ticker_raw.endswith("USDT") or not ticker_raw.endswith("USD"):
                continue
            
            # Remove X: prefix
            display_symbol = ticker_raw.replace("X:", "").strip()
            # Extract base symbol for deduplication and prioritization
            base_symbol = display_symbol.replace("USD", "").strip()
            if not display_symbol or not display_symbol.endswith("USD") or base_symbol in seen:
                continue
            seen.add(base_symbol)
            
            # Symbol should already end with USD, but ensure it does
            if not display_symbol.endswith("USD"):
                display_symbol = f"{base_symbol}USD"
            
            name = str(item.get("name") or "").strip()
            if not name:
                # Use friendly name from PolygonClient if available
                name = client.get_company_name(base_symbol) or f"{base_symbol} (Crypto)"
            
            ticker_info = {"symbol": display_symbol, "name": name}
            
            # Prioritize common cryptos
            if base_symbol in common_cryptos:
                prioritized.append(ticker_info)
            else:
                others.append(ticker_info)
        
        # Combine: prioritized first, then others
        crypto_tickers = prioritized + others
        
        # Apply limit
        return crypto_tickers[:limit] if limit > 0 else crypto_tickers
        
    except Exception as e:
        logger.warning(f"Crypto ticker search failed: {e}")
        return []


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

DISCORD_EMBED_DISCLAIMER = "Disclaimer: Not financial advice. Trade at your own risk."


def _ensure_embed_disclaimer(embed):
    """Place disclaimer at the end of the message (embed footer). Does not mutate; returns copy."""
    if not embed:
        return embed
    e = dict(embed)
    disclaimer = DISCORD_EMBED_DISCLAIMER
    footer = e.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        footer_text = (footer.get("text") or "").rstrip()
        e["footer"] = {"text": footer_text + ("\n\n" if footer_text else "") + disclaimer}
    else:
        e["footer"] = {"text": disclaimer}
    return e


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

def send_to_discord(signal, file_attachment=None):
    """Send signal data to Discord channel using user's webhook. file_attachment: optional uploaded file (image/video) for Chart Analysis."""
    # Get the appropriate template based on signal type
    embed = get_signal_template(signal)
    embed = _ensure_embed_disclaimer(embed)

    if file_attachment:
        file_name = (getattr(file_attachment, "name", None) or "chart_analysis").strip() or "chart_analysis"
        content_type = getattr(file_attachment, "content_type", "") or "application/octet-stream"
        if content_type.startswith("image/"):
            embed["image"] = {"url": f"attachment://{file_name}"}
        # Video is sent as message attachment; no embed.video for webhook

    is_valid, _, validation_error = validate_embed(embed)
    if not is_valid:
        print(f"ERROR: Discord embed validation failed - {validation_error}")
        return False

    payload = {"content": "@everyone", "embeds": [embed]}

    # Try to get user's webhook - check for selected channel or default channel
    try:
        if signal.discord_channel and signal.discord_channel.is_active:
            url = signal.discord_channel.webhook_url
        else:
            default_channel = DiscordChannel.objects.filter(user=signal.user, is_default=True, is_active=True).first()
            if default_channel:
                url = default_channel.webhook_url
            else:
                first_channel = DiscordChannel.objects.filter(user=signal.user, is_active=True).first()
                if first_channel:
                    url = first_channel.webhook_url
                else:
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
    except Exception as e:
        print(f"ERROR: Failed to get Discord webhook: {e}")
        return False

    try:
        if file_attachment:
            file_attachment.seek(0)
            payload_json = json.dumps(payload, ensure_ascii=False)
            resp = requests.post(
                url,
                data={"payload_json": payload_json},
                files={"file": (file_name, file_attachment.read(), content_type)},
                timeout=30,
            )
        else:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        resp.raise_for_status()
        return True
    except requests.HTTPError as e:
        error_msg = response.json() if response.text else {}
        print(f"Failed to send to Discord: {e}")
        print(f"Status: {response.status_code}")
        print(f"Response: {error_msg}")
        
        # Provide specific guidance based on status code
        if response.status_code == 401:
            print("ERROR: Invalid webhook URL")
        elif resp and resp.status_code == 403:
            print("ERROR: Webhook lacks permissions")
        elif resp and resp.status_code == 404:
            print("ERROR: Webhook not found")
        elif resp and resp.status_code == 400:
            print("ERROR: Invalid webhook URL or bad request")
        
        return False
    except requests.RequestException as e:
        print(f"Failed to send to Discord: {e}")
        return False


def _send_discord_embed(url, embed):
    """POST a single embed to a Discord webhook URL. Returns True on success."""
    url = str(url or "").strip()
    if not url:
        return False
    try:
        e = _ensure_embed_disclaimer(embed or {})
        # @everyone in content (outside embed), not in embed footer
        payload = {"content": "@everyone", "embeds": [e]}
        resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
        resp.raise_for_status()
        return True
    except Exception:
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
            # Default when unavailable: 0.00
            return f"{price:.2f}" if isinstance(price, (int, float)) else "0.00"

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
                    return f"{float(val):.2f}"
                except Exception:
                    return "0.00"
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
    # When is_shares is on: remove Expiration, Strike, and Option Price fields from the Discord embed.
    if is_shares and isinstance(fields, list):
        def _is_option_only_field(f):
            n = str((f or {}).get("name") or "").strip().lower()
            return "expiration" in n or "strike" in n or "option price" in n
        fields = [f for f in fields if not _is_option_only_field(f)]

    # Inject Trade Plan for both options and shares so sent Discord message matches preview.
    if isinstance(fields, list):
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
            # Keep Targets summary plus the detailed Take Profit Plan.
            max_tp = 6
            def _row_exists(i: int) -> bool:
                for k in (f"tp{i}_mode", f"tp{i}_per", f"tp{i}_stock_price", f"tp{i}_takeoff_per"):
                    if str(data_copy.get(k) or "").strip() != "":
                        return True
                return False

            last_tp = 0
            for i in range(1, max_tp + 1):
                if _row_exists(i):
                    last_tp = i

            targets = []
            for i in range(1, (last_tp or 0) + 1):
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
                        sp_num = float(sp) if sp else 0.0
                        sp_str = f"{sp_num:.2f}"
                    except Exception:
                        sp_str = str(stock_raw).strip() or "0.00"
                    # Stock price based targets: show as "TP1. $285.62"
                    targets.append(f"TP{i}.${sp_str}")
                    continue

                per = str(data_copy.get(f"tp{i}_per") or "").strip()
                if not per:
                    continue
                try:
                    per_num = float(per.replace("%", "").strip())
                    per_fmt = f"{per_num:+.1f}%"
                except Exception:
                    per_fmt = per if per.endswith("%") else f"{per}%"

                price_raw = data_copy.get(f"tp{i}_price")
                try:
                    price_num = float(price_raw) if price_raw is not None and str(price_raw).strip() != "" else 0.0
                    price_fmt = f"{price_num:.2f}"
                except Exception:
                    price_fmt = "0.00"

                # % based targets: show as "$6.24 (+20.0%)" (no takeoff in summary)
                targets.append(f"${price_fmt} ({per_fmt})")

            tps = []
            def _pct1(v) -> str:
                try:
                    s = str(v or "").strip().replace("%", "")
                    if not s:
                        return ""
                    return f"{float(s):.1f}%"
                except Exception:
                    return ""

            trailing = _pct1(data_copy.get("sl_per")) or "15.0%"
            tp_steps: list[dict] = []
            for i in range(1, (last_tp or 0) + 1):
                mode = str(data_copy.get(f"tp{i}_mode") or "").strip().lower()
                take_pct = _pct1(data_copy.get(f"tp{i}_takeoff_per"))

                if mode in ("stock", "stock_price", "underlying", "share_price"):
                    stock_raw = data_copy.get(f"tp{i}_stock_price")
                    try:
                        sp = str(stock_raw or "").strip()
                        sp_num = float(sp) if sp else 0.0
                        # Stock price TP plan: include level label (e.g. "TP1.$0.00")
                        at_str = f"TP{i}.${sp_num:.2f}"
                    except Exception:
                        at_str = f"TP{i}.${str(stock_raw or '').strip() or '0.00'}"
                    tp_steps.append({"level": i, "at_str": at_str, "take_pct": take_pct})
                    continue

                per = _pct1(data_copy.get(f"tp{i}_per"))
                per = per or f"{float(i * 10):.1f}%"
                tp_steps.append({"level": i, "at_str": per, "take_pct": take_pct})

            def _ensure_period(s: str) -> str:
                s2 = str(s or "").strip()
                return s2 if s2.endswith(".") else f"{s2}."

            for idx, step in enumerate(tp_steps):
                at_str = str(step.get("at_str") or "").strip()
                level = int(step.get("level") or (idx + 1))
                take_pct = str(step.get("take_pct") or "").strip()
                if take_pct:
                    take_str = f" take off {take_pct} of position" if idx == 0 else f" take off {take_pct} of remaining position"
                else:
                    take_str = ""

                # Raise stop loss suffix from signal data
                suffix = ""
                raise_sl_to = str(data_copy.get(f"tp{level}_raise_sl_to") or "").strip().lower()
                if raise_sl_to == "entry":
                    suffix = " and raise stop loss to break even"
                elif raise_sl_to == "custom":
                    mode = str(data_copy.get(f"tp{level}_mode") or "").strip().lower()
                    is_stock = mode in ("stock", "stock_price", "underlying", "share_price")
                    custom_stock = str(data_copy.get(f"tp{level}_raise_sl_custom_stock") or "").strip()
                    custom_per = str(data_copy.get(f"tp{level}_raise_sl_custom_per") or "").strip()
                    custom_price = str(data_copy.get(f"tp{level}_raise_sl_custom") or "").strip()
                    if is_stock and custom_stock:
                        try:
                            suffix = f" and raise the stop loss to ${float(custom_stock):.2f} above the entry price"
                        except (TypeError, ValueError):
                            suffix = " and raise the stop loss to custom level"
                    elif custom_per:
                        try:
                            v = float(str(custom_per).replace("%", "").strip())
                            suffix = f" and raise the stop loss to {custom_per}% above the entry price" if v else ""
                        except (TypeError, ValueError):
                            suffix = ""
                    elif custom_price:
                        try:
                            v = float(custom_price)
                            suffix = f" and raise the stop loss to ${v:.2f} above the entry price" if v else ""
                        except (TypeError, ValueError):
                            suffix = ""
                    else:
                        suffix = ""

                tps.append(_ensure_period(f"Take Profit ({level}): At {at_str}{take_str}{suffix}"))

            sl_per = str(data_copy.get("sl_per") or "").strip()
            sl_per_str = sl_per if (sl_per and sl_per.endswith("%")) else (f"{sl_per}%" if sl_per else "")
            sl_price_raw = data_copy.get("sl_price")
            try:
                sl_price_str = f"{float(sl_price_raw):.2f}" if sl_price_raw is not None and str(sl_price_raw).strip() != "" else "0.00"
            except Exception:
                sl_price_str = "0.00"
            
            # Collect stop loss levels from "raise stop loss to" settings in TP plan
            stop_loss_levels_from_tp = []
            entry_price_raw = data_copy.get("entry_price") or data_copy.get("current_price") or data_copy.get("price") or ""
            try:
                entry_price = float(str(entry_price_raw).strip()) if entry_price_raw else 0.0
            except (ValueError, TypeError):
                entry_price = 0.0
            
            # Compute initial stop loss price from percentage when price is zero or missing
            try:
                _sl_per_num = float(str(sl_per).replace("%", "").strip()) if sl_per else None
            except (ValueError, TypeError):
                _sl_per_num = None
            _effective_initial_sl = None  # display/compute: explicit price or from %
            if sl_price_raw is not None and str(sl_price_raw).strip() != "":
                try:
                    _effective_initial_sl = float(str(sl_price_raw).strip())
                except (ValueError, TypeError):
                    pass
            if _effective_initial_sl is None and _sl_per_num is not None and entry_price > 0:
                _effective_initial_sl = entry_price * (1 + _sl_per_num / 100.0)
            if _effective_initial_sl is not None:
                sl_price_str = f"{_effective_initial_sl:.2f}"
            
            # Get TP prices for calculating stop loss levels
            tp_prices = {}
            # Determine if this is a shares trade
            is_shares = str(data_copy.get("is_shares") or "").strip().lower() in ("true", "1", "yes")
            # Get base price for calculations
            base_price_raw = None
            if is_shares:
                base_price_raw = data_copy.get("current_price") or data_copy.get("stock_price") or entry_price_raw
            else:
                base_price_raw = data_copy.get("option_price") or data_copy.get("price") or entry_price_raw
            try:
                base_price = float(str(base_price_raw).strip()) if base_price_raw else entry_price
            except (ValueError, TypeError):
                base_price = entry_price
            
            # Calculate TP prices in order (needed for "raise to TP{n-1}")
            # First pass: get stored prices
            for i in range(1, (last_tp or 0) + 1):
                tp_price_raw = data_copy.get(f"tp{i}_price")
                tp_stock_price_raw = data_copy.get(f"tp{i}_stock_price")
                tp_mode_raw = str(data_copy.get(f"tp{i}_mode") or "").strip().lower()
                is_stock_mode = tp_mode_raw in ("stock", "stock_price", "underlying", "share_price")
                
                if is_stock_mode and tp_stock_price_raw:
                    try:
                        tp_price = float(str(tp_stock_price_raw).strip())
                        if tp_price > 0:
                            tp_prices[i] = tp_price
                    except (ValueError, TypeError):
                        pass
                elif tp_price_raw:
                    try:
                        tp_price = float(str(tp_price_raw).strip())
                        if tp_price > 0:
                            tp_prices[i] = tp_price
                    except (ValueError, TypeError):
                        pass
            
            # Second pass: calculate missing TP prices from percentages
            for i in range(1, (last_tp or 0) + 1):
                if i in tp_prices:
                    continue  # Already have price
                tp_mode_raw = str(data_copy.get(f"tp{i}_mode") or "").strip().lower()
                is_stock_mode = tp_mode_raw in ("stock", "stock_price", "underlying", "share_price")
                if is_stock_mode:
                    continue  # Stock mode prices should be stored, skip calculation
                
                # Calculate TP price from percentage if not stored
                tp_per_raw = data_copy.get(f"tp{i}_per")
                if tp_per_raw and base_price > 0:
                    try:
                        tp_per = float(str(tp_per_raw).replace("%", "").strip())
                        if tp_per != 0:
                            # Calculate TP price: base * (1 + percentage/100)
                            tp_price = base_price * (1 + tp_per / 100.0)
                            if tp_price > 0:
                                tp_prices[i] = tp_price
                    except (ValueError, TypeError):
                        pass
            
            # Collect stop loss levels from raise stop loss to settings
            for i in range(1, (last_tp or 0) + 1):
                raise_sl_to = str(data_copy.get(f"tp{i}_raise_sl_to") or "").strip().lower()
                
                # Skip if raise_sl_to is not set (empty, "off", etc.)
                if not raise_sl_to or raise_sl_to == "off":
                    continue
                
                if raise_sl_to == "entry":
                    if i == 1 and entry_price > 0:
                        # TP1: raise to entry price
                        stop_loss_levels_from_tp.append(entry_price)
                    elif i > 1 and tp_prices.get(i - 1):
                        # TP2+: raise to previous TP price
                        stop_loss_levels_from_tp.append(tp_prices[i - 1])
                elif raise_sl_to == "custom":
                    mode_raw = str(data_copy.get(f"tp{i}_mode") or "").strip().lower()
                    is_stock = mode_raw in ("stock", "stock_price", "underlying", "share_price")
                    
                    custom_stock = str(data_copy.get(f"tp{i}_raise_sl_custom_stock") or "").strip()
                    custom_per = str(data_copy.get(f"tp{i}_raise_sl_custom_per") or "").strip()
                    custom_price = str(data_copy.get(f"tp{i}_raise_sl_custom") or "").strip()
                    
                    if is_stock and custom_stock:
                        try:
                            stock_val = float(custom_stock)
                            if stock_val > 0 and entry_price > 0:
                                stop_loss_levels_from_tp.append(entry_price + stock_val)
                        except (ValueError, TypeError):
                            pass
                    elif custom_price:
                        try:
                            price_val = float(custom_price)
                            if price_val > 0 and entry_price > 0:
                                stop_loss_levels_from_tp.append(entry_price + price_val)
                        except (ValueError, TypeError):
                            pass
                    elif custom_per:
                        try:
                            per_val = float(str(custom_per).replace("%", "").strip())
                            if per_val > 0 and entry_price > 0:
                                stop_loss_levels_from_tp.append(entry_price * (1 + per_val / 100))
                        except (ValueError, TypeError):
                            pass
            
            # Combine manual sl_levels with calculated levels from TP plan
            all_stop_loss_levels = []
            
            # Add initial stop loss price (explicit or computed from percentage, including 0)
            if _effective_initial_sl is not None:
                all_stop_loss_levels.append(_effective_initial_sl)
            
            # Parse manual sl_levels
            sl_levels_raw = str(data_copy.get("sl_levels") or "").strip()
            if sl_levels_raw:
                manual_levels = []
                for level_str in sl_levels_raw.split(";"):
                    level_str = level_str.strip()
                    if not level_str:
                        continue
                    try:
                        num = float(level_str.replace("$", "").replace(",", "").strip())
                        if num > 0:
                            manual_levels.append(num)
                    except (ValueError, TypeError):
                        pass
                # Add manual levels (avoid duplicates)
                for level in manual_levels:
                    is_duplicate = any(abs(existing - level) < 0.01 for existing in all_stop_loss_levels)
                    if not is_duplicate:
                        all_stop_loss_levels.append(level)
            
            # Add calculated levels from TP plan (avoid duplicates)
            for level in stop_loss_levels_from_tp:
                if level > 0:
                    # Check if level is already in the list (within 0.01 tolerance)
                    is_duplicate = any(abs(existing - level) < 0.01 for existing in all_stop_loss_levels)
                    if not is_duplicate:
                        all_stop_loss_levels.append(level)
            
            # Sort levels (highest to lowest for stop losses)
            all_stop_loss_levels.sort(reverse=True)
            
            # Format stop loss levels with price and percentage relative to entry price
            sl_levels_formatted = ""
            if all_stop_loss_levels:
                formatted_levels = []
                for level in all_stop_loss_levels:
                    price_str = f"{level:.2f}"
                    if entry_price > 0:
                        percent = ((level - entry_price) / entry_price) * 100
                        percent_str = f"{percent:+.1f}%"
                    else:
                        percent_str = "0%"
                    formatted_levels.append(f"{price_str}({percent_str})")
                sl_levels_formatted = ", ".join(formatted_levels)

            # Show Trade Plan even if option price isn't computed yet (defaults to 0.00)
            if targets or tps or sl_per_str or sl_price_str or sl_levels_formatted:
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
                if targets:
                    joiner = ",  " if any(str(t or "").startswith("TP") for t in targets) else ", "
                    injected.append({"name": f"🎯 Targets: {joiner.join(targets)}", "value": "", "inline": False})
                
                # Stop Loss: show multiple levels if provided, otherwise single price or percentage
                if sl_levels_formatted:
                    stop_loss_name = f"🛑 Stop Loss: {sl_levels_formatted}"
                elif sl_price_str and sl_price_str != "0.00":
                    # Calculate percentage for single stop loss price
                    try:
                        sl_price = float(sl_price_str)
                        if entry_price > 0:
                            percent = ((sl_price - entry_price) / entry_price) * 100
                            percent_str = f"{percent:+.1f}%"
                        else:
                            percent_str = f"({sl_per_str})" if sl_per_str else ""
                        stop_loss_name = f"🛑 Stop Loss: {sl_price_str}({percent_str})" if percent_str else f"🛑 Stop Loss: {sl_price_str}"
                    except (ValueError, TypeError):
                        stop_loss_name = f"🛑 Stop Loss: {sl_price_str}{f'({sl_per_str})' if sl_per_str else ''}"
                elif sl_per_str:
                    # Show stop loss percentage even if no price is set yet
                    stop_loss_name = f"🛑 Stop Loss: {sl_per_str}"
                else:
                    stop_loss_name = None
                
                if stop_loss_name:
                    injected.append(
                        {
                            "name": stop_loss_name,
                            "value": "",
                            "inline": False,
                        }
                    )
                
                # Trailing Stop: show when trigger is not "none"
                trailing_stop_trigger = str(data_copy.get("trailing_stop_trigger") or "").strip().lower() or "none"
                trailing_stop_per_raw = str(data_copy.get("trailing_stop_per") or "").strip()
                if trailing_stop_trigger != "none" and trailing_stop_per_raw:
                    trailing_stop_per_str = trailing_stop_per_raw if trailing_stop_per_raw.endswith("%") else f"{trailing_stop_per_raw}%"
                    trigger_labels = {
                        "entry": "Entry",
                        "tp1": "Take Profit 1",
                        "tp2": "Take Profit 2",
                        "tp3": "Take Profit 3",
                        "tp4": "Take Profit 4",
                        "tp5": "Take Profit 5",
                        "tp6": "Take Profit 6",
                        "custom": "Custom"
                    }
                    trigger_label = trigger_labels.get(trailing_stop_trigger, trailing_stop_trigger)
                    injected.append(
                        {
                            "name": f"📉 Trailing Stop: {trailing_stop_per_str} (trigger: {trigger_label})",
                            "value": "",
                            "inline": False,
                        }
                    )
                
                # Time Horizon (for leaps/swings only; under Stop Loss in Trade Plan)
                time_horizon_raw = str(data_copy.get("time_horizon") or "").strip()
                trade_type_raw = str(data_copy.get("trade_type") or "").strip().lower()
                if time_horizon_raw and trade_type_raw in ("swing", "leap"):
                    injected.append(
                        {
                            "name": f"⏱️ Time Horizon: {time_horizon_raw}",
                            "value": "",
                            "inline": False,
                        }
                    )
                if tps:
                    injected.append({"name": "", "value": "\u200b", "inline": False})
                    injected.append({"name": "💰 Take Profit Plan", "value": "\n".join(tps), "inline": False})

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
@require_http_methods(["GET", "POST"])
def agreement(request):
    """Show current agreement and record acceptance on POST."""
    current = Agreement.objects.filter(is_active=True).order_by("-published_at", "-id").first()
    if not current:
        return redirect("dashboard")

    next_url = (request.GET.get("next") or request.POST.get("next") or "").strip() or reverse("dashboard")

    if request.method == "POST":
        if request.POST.get("agree"):
            AgreementAcceptance.objects.get_or_create(agreement=current, user=request.user)
            return redirect(next_url)
        # No agree: fall through to re-show the page

    return render(
        request,
        "signals/agreement.html",
        {"agreement": current, "next": next_url if next_url != reverse("dashboard") else None},
    )


@login_required
@require_http_methods(["GET", "POST"])
def post_ta(request):
    """Post TA: upload image/video and commentary to a Discord channel via webhook."""
    discord_channels = DiscordChannel.objects.filter(
        user=request.user, is_active=True
    ).order_by("-is_default", "channel_name")

    if request.method == "GET":
        return render(
            request,
            "signals/post_ta.html",
            {"discord_channels": discord_channels},
        )

    # POST
    channel_id_raw = (request.POST.get("discord_channel") or "").strip()
    commentary = (request.POST.get("commentary") or "").strip()
    ta_file = request.FILES.get("ta_media")

    if not channel_id_raw:
        messages.error(request, "Please select a Discord channel.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    try:
        channel_id = int(channel_id_raw)
    except (ValueError, TypeError):
        messages.error(request, "Invalid channel.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    try:
        channel = DiscordChannel.objects.get(id=channel_id, user=request.user, is_active=True)
    except DiscordChannel.DoesNotExist:
        messages.error(request, "Channel not found.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    if not ta_file:
        messages.error(request, "Please select an image or video file.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    # Discord webhook file size limit is 25MB; keep a safe limit (e.g. 8MB)
    max_bytes = 8 * 1024 * 1024
    if ta_file.size > max_bytes:
        messages.error(request, "File is too large. Keep under 8 MB.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    content_type = getattr(ta_file, "content_type", "") or ""
    if not content_type.startswith("image/") and not content_type.startswith("video/"):
        messages.error(request, "Only image or video files are allowed.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    # Build Discord embed: title, description (commentary), optional image in embed
    description = (commentary or "").strip()
    if len(description) > DISCORD_EMBED_DESCRIPTION_MAX_CHARS:
        description = description[: DISCORD_EMBED_DESCRIPTION_MAX_CHARS - 3] + "..."
    if not description:
        description = "\u2014"  # em dash so embed has visible body

    embed = {
        "title": "Technical Analysis",
        "description": description,
        "color": 0x5865F2,  # Discord blurple
        "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    # Use one filename for both embed reference and form attachment so they match
    file_name = (ta_file.name or "ta_media").strip() or "ta_media"
    # Show uploaded image inside the embed when it's an image (video stays as message attachment)
    if content_type.startswith("image/"):
        embed["image"] = {"url": f"attachment://{file_name}"}
    embed = _ensure_embed_disclaimer(embed)

    is_valid, _, embed_error = validate_embed(embed)
    if not is_valid:
        messages.error(request, embed_error or "Content too long for Discord.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    # @everyone in content (outside embed), not in embed footer
    payload = {"content": "@everyone", "embeds": [embed]}
    payload_json = json.dumps(payload)
    try:
        ta_file.seek(0)
        resp = requests.post(
            channel.webhook_url,
            data={"payload_json": payload_json},
            files={"file": (file_name, ta_file.read(), content_type)},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.exception("Post TA webhook failed: %s", e)
        messages.error(request, "Failed to send to Discord. Check your webhook and try again.")
        return render(request, "signals/post_ta.html", {"discord_channels": discord_channels})

    messages.success(request, "TA posted to Discord.")
    return redirect("post_ta")


@login_required
@require_GET
def saved_trade_plans(request):
    """List the user's saved trade plan presets as cards."""
    qs = UserTradePlanPreset.objects.filter(user=request.user).order_by(
        "-is_default", "-updated_at", "name"
    )
    plan_cards = []
    for p in qs:
        plan = p.plan if isinstance(p.plan, dict) else {}
        tp_mode_raw = str(plan.get("tp_mode") or "").strip().lower()
        if tp_mode_raw in ("stock", "stock_price", "underlying", "share_price"):
            tp_mode_display = "Stock price"
        elif tp_mode_raw in ("percent", "pct", "%"):
            tp_mode_display = "Percentage"
        else:
            tp_mode_display = "Percentage"
        tp_levels = plan.get("tp_levels") or []
        sl_per = str(plan.get("sl_per") or "").strip()
        parts = []
        for i, lev in enumerate(tp_levels[:3]):
            if isinstance(lev, dict):
                per = str(lev.get("per") or "").strip()
                sp = str(lev.get("stock_price") or "").strip()
                if sp:
                    parts.append(f"TP{i + 1}: ${sp}")
                elif per:
                    parts.append(f"TP{i + 1}: {per}%")
        if len(tp_levels) > 3:
            parts.append("…")
        if sl_per:
            parts.append(f"SL: {sl_per}%")
        preview_text = " · ".join(parts) if parts else "No targets set"
        # Discord embed matching New Trade Plan Live Preview (stacked fields, same labels)
        embed_color = 0x5865F2
        embed_title = "📝 Trade Plan"
        targets_parts = []
        tp_plan_lines = []
        for i, lev in enumerate(tp_levels or []):
            if not isinstance(lev, dict):
                continue
            per = str(lev.get("per") or "").strip()
            sp = str(lev.get("stock_price") or "").strip()
            takeoff = str(lev.get("takeoff") or "").strip()
            takeoff_fmt = f"{takeoff}%" if takeoff and not takeoff.endswith("%") else (takeoff or "")
            if sp:
                targets_parts.append(f"TP{i + 1}: ${sp}")
                at_str = f"TP{i + 1}.${sp}"
            elif per:
                per_clean = per.replace("%", "").strip()
                targets_parts.append(f"TP{i + 1}: {per_clean}%")
                at_str = f"{per_clean}%" if per_clean else f"{(i + 1) * 10}%"
            else:
                continue
            take_str = f" take off {takeoff_fmt} of position" if takeoff_fmt else ""
            if i > 0 and take_str:
                take_str = take_str.replace(" of position", " of remaining position")
            raise_sl_suffix = ""
            raise_sl_to = str(lev.get("raise_sl_to") or "").strip().lower()
            lev_num = i + 1
            if raise_sl_to == "entry":
                raise_sl_suffix = " and raise stop loss to break even (entry)" if lev_num == 1 else f" and raise stop loss to TP{lev_num - 1}"
            elif raise_sl_to == "custom":
                custom_stock = str(lev.get("raise_sl_custom_stock") or "").strip()
                custom_per = str(lev.get("raise_sl_custom_per") or "").strip()
                custom_price = str(lev.get("raise_sl_custom") or "").strip()
                if sp and custom_stock:
                    try:
                        v = float(custom_stock)
                        raise_sl_suffix = f" and raise the stop loss to ${v:.2f} above the entry price" if v else ""
                    except (TypeError, ValueError):
                        raise_sl_suffix = ""
                elif custom_per:
                    try:
                        v = float(str(custom_per).replace("%", "").strip())
                        raise_sl_suffix = f" and raise the stop loss to {custom_per}% above the entry price" if v else ""
                    except (TypeError, ValueError):
                        raise_sl_suffix = ""
                else:
                    raise_sl_suffix = " and raise the stop loss to entry price" if v else ""
            tp_plan_lines.append(f"Take Profit ({i + 1}): At {at_str}{take_str}{raise_sl_suffix}.")
        sl_display = f"{sl_per}%" if sl_per and not str(sl_per).endswith("%") else (sl_per or "")
        embed_fields = []
        if targets_parts:
            joiner = ",  " if any("TP" in t for t in targets_parts) else ",  "
            embed_fields.append({"name": f"🎯 Targets: {joiner.join(targets_parts)}", "value": "", "inline": False})
        if sl_display:
            embed_fields.append({"name": f"🛑 Stop Loss: {sl_display}", "value": "", "inline": False})
        if tp_plan_lines:
            embed_fields.append({"name": "", "value": "\u200b", "inline": False})
            embed_fields.append({"name": "💰 Take Profit Plan", "value": "\n".join(tp_plan_lines), "inline": False})
        if not embed_fields:
            embed_fields.append({"name": "—", "value": "No targets set", "inline": False})
        discord_embed = {
            "title": embed_title,
            "description": "",
            "fields": embed_fields,
            "color_hex": f"#{embed_color:06x}",
        }
        plan_cards.append({
            "id": p.id,
            "name": p.name,
            "is_default": bool(p.is_default),
            "tp_mode": tp_mode_display,
            "preview_text": preview_text,
            "discord_embed": discord_embed,
        })
    return render(
        request,
        "signals/saved_trade_plans.html",
        {"plan_cards": plan_cards},
    )


@login_required
@require_GET
def new_trade_plan(request):
    """New Trade Plan page: dashboard template with only Trade Plan panel and preview (trade_plan_only=True)."""
    form = SignalForm(user=request.user)
    recent_signals = []
    signal_types = SignalType.objects.filter(
        Q(user__isnull=True) | Q(user=request.user)
    )
    signal_types_data = []
    for st in signal_types:
        signal_types_data.append({
            "id": st.id,
            "name": st.name or "",
            "variables": st.variables or [],
            "title_template": st.title_template or "",
            "description_template": st.description_template or "",
            "footer_template": st.footer_template or "",
            "color": st.color or "#000000",
            "fields_template": st.fileds_template or [],
            "show_title_default": getattr(st, "show_title_default", True),
            "show_description_default": getattr(st, "show_description_default", True),
        })
    discord_channels = DiscordChannel.objects.filter(
        user=request.user, is_active=True
    ).order_by("-is_default", "channel_name")
    presets = []
    try:
        qs = UserTradePlanPreset.objects.filter(user=request.user).order_by(
            "-is_default", "-updated_at", "name"
        )
        for p in qs:
            presets.append({
                "id": p.id,
                "name": p.name,
                "plan": p.plan if isinstance(p.plan, dict) else {},
                "is_default": bool(p.is_default),
            })
    except Exception:
        presets = []

    return render(request, "signals/dashboard.html", {
        "form": form,
        "recent_signals": recent_signals,
        "signal_types_data": signal_types_data,
        "discord_channels": discord_channels,
        "trade_plan_presets": presets,
        "trade_plan_only": True,
    })


def _get_dashboard_context(request, form):
    """Build context dict for dashboard template (form, signal_types_data, discord_channels, presets)."""
    recent_signals = []
    signal_types = SignalType.objects.filter(Q(user__isnull=True) | Q(user=request.user))
    signal_types_data = [
        {
            'id': st.id,
            'name': st.name or '',
            'variables': st.variables or [],
            'title_template': st.title_template or '',
            'description_template': st.description_template or '',
            'footer_template': st.footer_template or '',
            'color': st.color or '#000000',
            'fields_template': st.fileds_template or [],
            'show_title_default': getattr(st, 'show_title_default', True),
            'show_description_default': getattr(st, 'show_description_default', True),
        }
        for st in signal_types
    ]
    discord_channels = DiscordChannel.objects.filter(user=request.user, is_active=True).order_by('-is_default', 'channel_name')
    presets = []
    try:
        for p in UserTradePlanPreset.objects.filter(user=request.user).order_by("-is_default", "-updated_at", "name"):
            presets.append({"id": p.id, "name": p.name, "plan": p.plan if isinstance(p.plan, dict) else {}, "is_default": bool(p.is_default)})
    except Exception:
        pass
    return {
        'form': form,
        'recent_signals': recent_signals,
        'signal_types_data': signal_types_data,
        'discord_channels': discord_channels,
        'trade_plan_presets': presets,
    }


@login_required
def dashboard(request):
    if request.method == 'POST':
        form = SignalForm(request.POST, request.FILES, user=request.user)
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

            # Chart Analysis (optional file): if user uploaded image/video, mark for embed and validate
            chart_file = request.FILES.get('chart_analysis')
            if chart_file:
                signal_data['chart_analysis'] = '[Attachment included]'
                max_bytes = 8 * 1024 * 1024
                if chart_file.size > max_bytes:
                    messages.error(request, 'Chart Analysis file is too large. Keep under 8 MB.')
                    return render(request, 'signals/dashboard.html', _get_dashboard_context(request, form))
                ct = getattr(chart_file, 'content_type', '') or ''
                if not (ct.startswith('image/') or ct.startswith('video/')):
                    messages.error(request, 'Chart Analysis: only image or video files are allowed.')
                    return render(request, 'signals/dashboard.html', _get_dashboard_context(request, form))

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
                                try:
                                    price_f = float(underlying_price)
                                    strike_gte = round(price_f * 0.5, 2)
                                    strike_lte = round(price_f * 2.0, 2)
                                except (TypeError, ValueError):
                                    strike_gte = strike_lte = None

                                best, _ = client.get_best_option(
                                    underlying=sym,
                                    side=side,
                                    expiration_gte=exp_gte,
                                    expiration_lte=exp_lte,
                                    strike_gte=strike_gte,
                                    strike_lte=strike_lte,
                                    underlying_price=float(underlying_price),
                                    trade_type=tt,
                                    timeout=30,
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
                                    signal_data["option_price"] = f"{opt_price_f:.2f}"
                                    # Also populate common price fields for legacy templates
                                    if _is_zero_price(signal_data.get("price")):
                                        signal_data["price"] = f"{opt_price_f:.2f}"
                                    if _is_zero_price(signal_data.get("entry_price")):
                                        signal_data["entry_price"] = f"{opt_price_f:.2f}"

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
                                            signal_data[f"tp{i}_price"] = f"{(opt_price_f * (1.0 + per / 100.0)):.2f}"
                                    sl_per = _get_per("sl_per")
                                    if sl_per > 0:
                                        signal_data["sl_price"] = f"{(opt_price_f * (1.0 - sl_per / 100.0)):.2f}"
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
                # Open a position for this trade so it appears in Position Management
                try:
                    is_shares = _truthy(signal_data.get('is_shares', False))
                    symbol = (str(signal_data.get('ticker') or signal_data.get('symbol') or '').strip().upper() or '')[:20]
                    instrument = Position.INSTRUMENT_SHARES if is_shares else Position.INSTRUMENT_OPTIONS
                    # When is_shares: entry must be stock price only (never option_price). When options: use option_price.
                    if instrument == Position.INSTRUMENT_SHARES:
                        entry_raw = (
                            signal_data.get('current_price')
                            or signal_data.get('entry_price')
                            or signal_data.get('price')
                            or signal_data.get('stock_price')
                            or ''
                        )
                    else:
                        entry_raw = (
                            signal_data.get('option_price')
                            or signal_data.get('entry_price')
                            or signal_data.get('price')
                            or ''
                        )
                    try:
                        entry_price = Decimal(str(entry_raw).strip()) if entry_raw else None
                    except (InvalidOperation, TypeError):
                        entry_price = None
                    # For shares: if form didn't include a price, fetch current stock price so position entry is not empty
                    if instrument == Position.INSTRUMENT_SHARES and (entry_price is None or entry_price <= 0) and symbol:
                        stock_price = _get_stock_price(symbol)
                        if stock_price is not None and stock_price > 0:
                            entry_price = Decimal(str(round(stock_price, 2)))
                    # Shares: QTY = number of shares (1), multiplier 1 → display_qty 1. Options: quantity = contracts, multiplier 100 → display_qty = quantity*100.
                    if instrument == Position.INSTRUMENT_SHARES:
                        position_quantity = 100
                        position_multiplier = 1
                    else:
                        position_multiplier = 100
                        position_quantity = 1
                        try:
                            q = signal_data.get('quantity')
                            if q is not None and str(q).strip() != '':
                                position_quantity = max(1, int(Decimal(str(q))))
                        except (ValueError, InvalidOperation, TypeError):
                            pass
                    position_mode = (request.POST.get("position_mode") or "").strip().lower()
                    mode = Position.MODE_AUTO if position_mode == "auto" else Position.MODE_MANUAL
                    Position.objects.create(
                        user=request.user,
                        signal=signal_instance,
                        status=Position.STATUS_OPEN,
                        mode=mode,
                        symbol=symbol,
                        instrument=instrument,
                        option_contract=str(signal_data.get('option_contract') or '')[:64],
                        option_type=str(signal_data.get('option_type') or '').strip().upper()[:10],
                        strike=str(signal_data.get('strike') or '')[:32],
                        expiration=str(signal_data.get('expiration') or '')[:32],
                        quantity=position_quantity,
                        multiplier=position_multiplier,
                        entry_price=entry_price,
                    )
                    # Set mode from form: "auto" = automatic tracking (system checks TP/SL and posts exit)
                    position_mode = (request.POST.get("position_mode") or "").strip().lower()
                    if position_mode == "auto":
                        created = Position.objects.filter(user=request.user, signal=signal_instance).order_by("-id").first()
                        if created:
                            created.mode = Position.MODE_AUTO
                            created.save(update_fields=["mode"])
                except Exception as e:
                    logger.warning('Could not create position for signal %s: %s', signal_instance.id, e)
                success = send_to_discord(signal_instance, file_attachment=chart_file)
                
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
    
    return render(request, 'signals/dashboard.html', _get_dashboard_context(request, form))


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
        tp_mode_raw = str(plan_obj.get("tp_mode") or "").strip().lower()
        tp_mode = "stock" if tp_mode_raw in ("stock", "stock_price", "underlying", "share_price") else ("percent" if tp_mode_raw in ("percent", "pct", "%") else "")
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
            raise_sl_to = str(item.get("raise_sl_to") or "").strip().lower()
            if raise_sl_to not in ("", "off", "entry", "break_even", "custom"):
                raise_sl_to = ""
            if raise_sl_to == "off":
                raise_sl_to = ""
            raise_sl_custom = str(item.get("raise_sl_custom") or "").strip()
            raise_sl_custom_per = str(item.get("raise_sl_custom_per") or "").strip()
            raise_sl_custom_stock = str(item.get("raise_sl_custom_stock") or "").strip()
            level_dict = {"mode": mode or "percent", "per": per, "stock_price": stock_price, "takeoff": takeoff}
            if raise_sl_to:
                level_dict["raise_sl_to"] = raise_sl_to
            if raise_sl_custom:
                level_dict["raise_sl_custom"] = raise_sl_custom
            if raise_sl_custom_per:
                level_dict["raise_sl_custom_per"] = raise_sl_custom_per
            if raise_sl_custom_stock:
                level_dict["raise_sl_custom_stock"] = raise_sl_custom_stock
            cleaned_levels.append(level_dict)
        # Normalize takeoff: last TP = 100%, others = 50%.
        if cleaned_levels:
            for i in range(len(cleaned_levels) - 1):
                cleaned_levels[i]["takeoff"] = "50"
            cleaned_levels[-1]["takeoff"] = "100"

        sl_per_str = str(sl_per or "").strip()
        # If tp_mode wasn't provided, infer it from levels (stock wins).
        if not tp_mode:
            tp_mode = "stock" if any(
                str(l.get("mode") or "").startswith("stock")
                or str(l.get("mode") or "") == "underlying"
                or bool(str(l.get("stock_price") or "").strip())
                for l in cleaned_levels
            ) else "percent"
        return {"version": 1, "tp_mode": tp_mode, "tp_levels": cleaned_levels, "sl_per": sl_per_str}

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


def _exp_display(expiration_str):
    """Format expiration string to MM/DD for display (e.g. 2025-10-31 -> 10/31)."""
    s = str(expiration_str or "").strip()
    if not s:
        return "-"
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s[:10], fmt)
            return dt.strftime("%m/%d")
        except Exception:
            continue
    if len(s) >= 5 and s[2] == "/":
        return s[:5]
    return s[:10] if len(s) >= 10 else s


def _get_auto_risk_management(data, entry, takeoff, tp_level):
    """
    Build auto Risk Management text for Partial Exit (TP): raise stop loss based on raise_sl_to setting.
    Checks tp{tp_level}_raise_sl_to: "off", "entry", or "custom".
    - off: "Maintaining current stop loss on final X% runner position."
    - not set: previous level logic (TP1 → entry, TP2 → TP1, etc.)
    - entry/break_even: raise to break even
    - custom: use custom price/percent/stock
    """
    def _to_float(v):
        try:
            s = str(v or "").strip().replace("%", "")
            return float(s) if s else 0.0
        except (TypeError, ValueError):
            return 0.0

    remaining = max(0, min(100, 100.0 - float(takeoff)))
    
    # Check for raise_sl_to setting in trade plan (from Edit Parameters or trade plan)
    raise_sl_to_raw = data.get(f"tp{tp_level}_raise_sl_to")
    raise_sl_to = str(raise_sl_to_raw).strip().lower() if raise_sl_to_raw else ""
    
    # Explicit "off" = maintain current stop loss
    if raise_sl_to == "off":
        if remaining > 0:
            return (
                f"Maintaining current stop loss on final {remaining:.0f}% runner position to secure gains while allowing room to run."
            )
        return ""
    
    # Not set = default: raise to previous level (TP1 → entry, TP2 → TP1, etc.)
    if not raise_sl_to:
        if tp_level == 1:
            prev_price = entry
            prev_label = "entry"
        else:
            # Support both option price and stock price for previous TP level
            prev_price = _to_float(
                data.get(f"tp{tp_level - 1}_price") or data.get(f"tp{tp_level - 1}_stock_price")
            )
            prev_label = f"TP{tp_level - 1}"
        
        if prev_price and prev_price > 0:
            return (
                f"Raising stop loss to previous level: ${prev_price:.2f} ({prev_label}) "
                f"on final {remaining:.0f}% runner position to secure gains while allowing room to run."
            )
        if remaining > 0:
            return f"Final {remaining:.0f}% runner position to secure gains while allowing room to run."
        return ""
    
    # If "entry" or "break_even", raise to entry price
    if raise_sl_to in ("entry", "break_even"):
        if entry and entry > 0:
            pct_above = ((entry - entry) / entry * 100) if entry > 0 else 0.0
            return (
                f"Raising stop loss to ${entry:.2f} (break even) "
                f"on final {remaining:.0f}% runner position to secure gains while allowing room to run."
            )
        if remaining > 0:
            return f"Raising stop loss to break even on final {remaining:.0f}% runner position to secure gains while allowing room to run."
        return ""
    
    # If "custom", use custom values
    if raise_sl_to == "custom":
        # Check if this is stock-based or percent-based by checking TP mode
        tp_mode_raw = str(data.get(f"tp{tp_level}_mode") or "").strip().lower()
        is_stock_mode = tp_mode_raw in ("stock", "stock_price", "underlying", "share_price")
        custom_stock = str(data.get(f"tp{tp_level}_raise_sl_custom_stock") or "").strip()
        custom_per = str(data.get(f"tp{tp_level}_raise_sl_custom_per") or "").strip()
        custom_price = str(data.get(f"tp{tp_level}_raise_sl_custom") or "").strip()
        
        if is_stock_mode and custom_stock:
            try:
                sl_price = _to_float(custom_stock)
                if sl_price > 0:
                    pct_above = ((sl_price - entry) / entry * 100) if entry > 0 else 0.0
                    pct_str = f"({pct_above:+.1f}% above entry)" if pct_above != 0 else ""
                    return (
                        f"Raising stop loss to ${sl_price:.2f} {pct_str} "
                        f"on final {remaining:.0f}% runner position to secure gains while allowing room to run."
                    )
            except (TypeError, ValueError):
                pass
        elif custom_per:
            try:
                pct_val = _to_float(custom_per)
                if pct_val != 0 and entry > 0:
                    sl_price = entry * (1 + pct_val / 100)
                    return (
                        f"Raising stop loss to ${sl_price:.2f} ({pct_val:+.1f}% above entry) "
                        f"on final {remaining:.0f}% runner position to secure gains while allowing room to run."
                    )
            except (TypeError, ValueError):
                pass
        elif custom_price:
            try:
                sl_price = _to_float(custom_price)
                if sl_price > 0 and entry > 0:
                    pct_above = ((sl_price - entry) / entry * 100)
                    return (
                        f"Raising stop loss to ${sl_price:.2f} ({pct_above:+.1f}% above entry) "
                        f"on final {remaining:.0f}% runner position to secure gains while allowing room to run."
                    )
            except (TypeError, ValueError):
                pass
        
        # Fallback if custom values are invalid
        if remaining > 0:
            return f"Raising stop loss to custom level on final {remaining:.0f}% runner position to secure gains while allowing room to run."
        return ""
    
    # Default fallback
    if remaining > 0:
        return f"Final {remaining:.0f}% runner position to secure gains while allowing room to run."
    return ""


def _get_auto_strategy_executed_full_tp(data, entry, tp_level, override_price=None):
    """
    Build Strategy Executed text for Full Exit (TP): Full Exit is always 100% at the current TP level.
    No Trailing Stop. Example:
      ✅ TP3 Exit (100%) : 10.72 (+30.0%)
      🟢 Average exit: $10.72 (+30.0% blended)
    """
    def _to_float(v):
        try:
            s = str(v or "").strip().replace("%", "")
            return float(s) if s else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _fmt_money(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "0.00"

    level = int(tp_level)
    if level < 1:
        return f"Full exit at TP{tp_level}."

    price = _to_float(data.get(f"tp{level}_price"))
    if override_price is not None:
        try:
            override_f = float(override_price)
            if override_f > 0:
                price = override_f
        except (TypeError, ValueError):
            pass
    if not price or not entry:
        return f"Full exit at TP{level}."

    pct = (price - entry) / entry * 100 if entry and entry > 0 else _to_float(data.get(f"tp{level}_per"))
    lines = [f"✅ TP{level} Exit (100%) : {_fmt_money(price)} ({pct:+.1f}%)"]
    icon = "🔴" if pct < 0 else "🟢"
    lines.append(f"{icon} Average exit: ${_fmt_money(price)} ({pct:+.1f}% blended)")
    return "\n".join(lines)


def _get_auto_strategy_executed_full_sl(data, entry, sl_price, override_price=None):
    """Build Strategy Executed text for Full Exit (SL): stop loss exit and blended %."""
    def _to_float(v):
        try:
            s = str(v or "").strip().replace("%", "")
            return float(s) if s else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _fmt_money(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "0.00"

    price = sl_price
    if override_price is not None:
        try:
            override_f = float(override_price)
            if override_f > 0:
                price = override_f
        except (TypeError, ValueError):
            pass

    if not price or not entry:
        return "Full exit at stop loss."

    pct = (price - entry) / entry * 100
    lines = [f"✅ Stop Loss Exit (100%) : {_fmt_money(price)} ({pct:+.1f}%)"]
    icon = "🔴" if pct < 0 else "🟢"
    lines.append(f"{icon} Average exit: ${_fmt_money(price)} ({pct:+.1f}% blended)")
    return "\n".join(lines)


def _build_position_update_embed(pos, *, kind, tp_level=None, override_price=None, next_steps=None, risk_management=None, strategy_executed=None, partial_exit=False):
    """Build Discord embed. partial_exit=True: TP only, add Risk Management (auto), no Strategy Executed. partial_exit=False (Full Exit): add Strategy Executed below Status (auto), no Risk Management."""
    from django.utils import timezone
    symbol = (pos.symbol or "").strip().upper() or "TRADE"
    data = pos.signal.data if (pos.signal and isinstance(getattr(pos.signal, "data", None), dict)) else {}
    data = data if isinstance(data, dict) else {}

    def _to_float(v):
        try:
            s = str(v or "").strip().replace("%", "")
            return float(s) if s else 0.0
        except Exception:
            return 0.0

    def _fmt_money(v):
        try:
            return f"{float(v):.2f}"
        except Exception:
            return "0.00"

    def _fmt_pct1(v):
        try:
            return f"{float(v):.1f}%"
        except Exception:
            return "0.0%"

    company = ""
    try:
        company = _get_company_name(symbol) or ""
    except Exception:
        company = ""
    company_part = f" ({company})" if company else ""
    is_shares = getattr(pos, "instrument", None) == Position.INSTRUMENT_SHARES
    if is_shares:
        entry_raw = data.get("current_price") or data.get("entry_price") or data.get("price") or ""
    else:
        expiration = str(data.get("expiration") or pos.expiration or "").strip()
        strike = str(data.get("strike") or pos.strike or "").strip()
        opt_type = str(data.get("option_type") or pos.option_type or "").strip().upper()
        strike_line = " ".join([p for p in [strike, opt_type] if p]).strip()
        entry_raw = data.get("entry_price") or data.get("option_price") or data.get("price") or ""
    entry = _to_float(entry_raw if (entry_raw != "" and entry_raw is not None) else pos.entry_price)
    now = timezone.localtime(timezone.now())
    date_str = now.strftime("%a %b %d")
    color = 0x10B981 if kind == "tp" else 0xEF4444
    embed = {"color": color}
    if is_shares:
        price_str = _fmt_money(entry) if entry > 0 else "-"
        embed["fields"] = [{"name": "💵 Entry (Stock)", "value": price_str, "inline": True}]
    else:
        exp_str = _exp_display(expiration) if expiration else "-"
        strike_str = strike_line or "-"
        price_str = _fmt_money(entry) if entry > 0 else "-"
        embed["fields"] = [
            {"name": "❌ Expiration", "value": exp_str, "inline": True},
            {"name": "✍️ Strike", "value": strike_str, "inline": True},
            {"name": "💵 Price", "value": price_str, "inline": True},
        ]
    try:
        override_f = float(override_price) if override_price is not None else None
    except (TypeError, ValueError):
        override_f = None
    if kind == "tp" and tp_level and tp_level > 0:
        _tp_raw = (data.get(f"tp{tp_level}_stock_price") or data.get(f"tp{tp_level}_price")) if is_shares else data.get(f"tp{tp_level}_price")
        tp_price = _to_float(_tp_raw)
        if override_f is not None and override_f > 0:
            tp_price = override_f
        tp_per = _to_float(data.get(f"tp{tp_level}_per"))
        takeoff = _to_float(data.get(f"tp{tp_level}_takeoff_per")) or 50.0
        _next_raw = (data.get(f"tp{tp_level + 1}_stock_price") or data.get(f"tp{tp_level + 1}_price")) if is_shares else data.get(f"tp{tp_level + 1}_price")
        next_price = _to_float(_next_raw)
        next_line = f"Let remaining {int(round(100 - takeoff))}% ride to TP{tp_level + 1} (${_fmt_money(next_price)})" if next_price > 0 else ""
        entry_str = f"${_fmt_money(entry)}" if entry > 0 else "-"
        tp_hit_str = f"${_fmt_money(tp_price)}" if tp_price > 0 else "-"
        profit_str = _fmt_pct1(tp_per)
        embed["title"] = f"🎯 {symbol} Take Profit {tp_level} HIT — {date_str}"
        embed["fields"].extend([
            {"name": "✅ Entry", "value": entry_str, "inline": True},
            {"name": f"🎯 TP{tp_level} Hit", "value": tp_hit_str, "inline": True},
            {"name": "💸 Profit", "value": profit_str, "inline": True},
        ])
        embed["description"] = f"🟢 Trade Performance:\nTicker: {symbol}{company_part}"
        mgmt_lines = ["🔍 Position Management:", f"✅ Reduce position by {int(round(takeoff))}% (lock in +{_fmt_pct1(tp_per)} on half)"]
        if next_line:
            mgmt_lines.append(f"🎯 {next_line}")
        # Partial exit: Status TP Zone Reached + Position Management + Risk Management
        if partial_exit:
            status_line = f"🚨 Status: TP{tp_level} Zone Reached 🚨"
            desc_after = status_line + "\n\n" + "\n".join(mgmt_lines)
            if next_steps and isinstance(next_steps, str) and next_steps.strip():
                desc_after += "\n\n📌 **Next steps:** " + next_steps.strip()
            risk_text = risk_management if risk_management and isinstance(risk_management, str) and risk_management.strip() else _get_auto_risk_management(data, entry, takeoff, tp_level)
            if risk_text:
                desc_after += "\n\n🛡️ Risk Management:\n" + risk_text
        else:
            # Full exit: Status Position Closed only (no Position Management), then Strategy Executed
            status_line = "🚨 Status: Position Closed 🚨"
            desc_after = status_line
            if next_steps and isinstance(next_steps, str) and next_steps.strip():
                desc_after += "\n\n📌 **Next steps:** " + next_steps.strip()
            strategy_text = strategy_executed if strategy_executed and isinstance(strategy_executed, str) and strategy_executed.strip() else _get_auto_strategy_executed_full_tp(data, entry, tp_level, override_price)
            desc_after += "\n\n🔍 Strategy Executed:\n" + strategy_text
        embed["description_after"] = desc_after
    else:
        _sl_raw = (data.get("sl_stock_price") or data.get("sl_price")) if is_shares else data.get("sl_price")
        sl_price = _to_float(_sl_raw)
        if override_f is not None and override_f > 0:
            sl_price = override_f
        sl_per = _to_float(data.get("sl_per"))
        entry_str = f"${_fmt_money(entry)}" if entry > 0 else "-"
        sl_hit_str = f"${_fmt_money(sl_price)}" if sl_price > 0 else "-"
        profit_str = _fmt_pct1(-abs(sl_per)) if sl_per > 0 else "-"
        embed["title"] = f"🎯 {symbol} Stop Loss HIT — {date_str}"
        embed["fields"].extend([
            {"name": "✅ Entry", "value": entry_str, "inline": True},
            {"name": "🎯 Stop Loss Hit", "value": sl_hit_str, "inline": True},
            {"name": "💸 Profit", "value": profit_str, "inline": True},
        ])
        embed["description"] = f"🟢 Trade Performance:\nTicker: {symbol}{company_part}"
        desc_after_sl = "🚨 Status: Stop Loss Triggered 🚨"
        strategy_text = strategy_executed if strategy_executed and isinstance(strategy_executed, str) and strategy_executed.strip() else _get_auto_strategy_executed_full_sl(data, entry, sl_price, override_price)
        desc_after_sl += "\n\n🔍 Strategy Executed:\n" + strategy_text
        embed["description_after"] = desc_after_sl
    return embed


def _get_position_current_price(pos, bypass_cache=False):
    """
    Return current market price for a position (stock or option), or None if unavailable.
    Used for automatic TP/SL tracking and position management live updates.
    For shares: tries quote first, then last trade as fallback.
    
    Args:
        pos: Position instance
        bypass_cache: If True, bypasses quote cache to get fresh prices (for live updates)
    """
    polygon_key = getattr(settings, "POLYGON_API_KEY", None) or ""
    if not polygon_key:
        logger.debug("Position current price: POLYGON_API_KEY not set")
        return None
    if not pos.symbol:
        logger.debug("Position current price: position has no symbol (id=%s)", getattr(pos, "id", None))
        return None
    try:
        client = PolygonClient(polygon_key)
        if pos.instrument == Position.INSTRUMENT_SHARES:
            price = client.get_share_current_price(pos.symbol, bypass_cache=bypass_cache)
            if price is not None:
                return price
            # Fallback: last trade (sometimes available when NBBO/snapshot is not)
            trade = client.get_last_trade(pos.symbol, bypass_cache=bypass_cache)
            if trade and trade.get("p") is not None:
                try:
                    return float(trade["p"])
                except (TypeError, ValueError):
                    pass
            logger.debug("Position current price: no quote or last trade for %s", pos.symbol)
            return None
        if pos.option_contract:
            q = client.get_option_quote(pos.option_contract, bypass_cache=bypass_cache)
            if q and q.get("price") is not None:
                return float(q["price"])
            logger.debug("Position current price: no option quote for %s", pos.option_contract)
            return None
        logger.debug("Position current price: unsupported instrument or missing option_contract (id=%s)", getattr(pos, "id", None))
    except Exception as e:
        logger.warning("Position current price failed for %s: %s", pos.symbol, e)
    return None


def _apply_position_exit(pos, kind, current_price=None, next_steps=None, risk_management=None, strategy_executed=None, partial_exit=False):
    """
    Apply a TP or SL exit. partial_exit=True: Partial Exit (TP, Risk Management). partial_exit=False: Full Exit (Strategy Executed below Status). Returns True on success.
    """
    next_tp = (pos.tp_hit_level or 0) + 1
    override = None
    if current_price is not None:
        try:
            override = float(current_price)
        except (TypeError, ValueError):
            pass
    embed = _build_position_update_embed(
        pos, kind=kind, tp_level=next_tp if kind == "tp" else None, override_price=override,
        next_steps=next_steps, risk_management=risk_management, strategy_executed=strategy_executed, partial_exit=partial_exit
    )
    desc_after = embed.pop("description_after", None)
    if desc_after:
        # For partial exit, add description_after as a field so it appears after the other fields (matching preview structure)
        # For full exit, merge into description as before
        if partial_exit:
            embed["fields"].append({"name": "", "value": desc_after, "inline": False})
        else:
            embed["description"] = (embed.get("description") or "") + "\n\n" + desc_after
    url = None
    if pos.signal and getattr(pos.signal, "discord_channel", None) and pos.signal.discord_channel.is_active:
        url = pos.signal.discord_channel.webhook_url
    if not url:
        ch = DiscordChannel.objects.filter(user=pos.user, is_default=True, is_active=True).first()
        if ch:
            url = ch.webhook_url
        if not url:
            ch = DiscordChannel.objects.filter(user=pos.user, is_active=True).first()
            url = ch.webhook_url if ch else None
    if url and not _send_discord_embed(url, embed):
        return False
    from django.utils import timezone
    now = timezone.now()
    if kind == "tp":
        pos.tp_hit_level = next_tp
        data = (pos.signal.data if pos.signal and isinstance(getattr(pos.signal, "data", None), dict) else {}) or {}
        takeoff_pct = 50.0
        try:
            t = data.get(f"tp{next_tp}_takeoff_per")
            if t is not None:
                takeoff_pct = float(str(t).strip().replace("%", "")) if str(t).strip() else 50.0
        except (TypeError, ValueError):
            pass
        total_units = (pos.quantity or 1) * (pos.multiplier or 100)
        closed_u = pos.closed_units or 0
        remaining = max(0, total_units - closed_u)
        add_units = int(round(remaining * takeoff_pct / 100.0))
        pos.closed_units = closed_u + add_units
        tp_price = None
        try:
            tp_price_val = data.get(f"tp{next_tp}_price")
            if tp_price_val is not None:
                tp_price = float(str(tp_price_val).strip())
        except (TypeError, ValueError):
            pass
        entry = float(pos.entry_price) if pos.entry_price is not None else 0
        update_fields = ["tp_hit_level", "closed_units", "updated_at"]
        if tp_price is not None and entry and add_units > 0:
            realized_this_tp = (tp_price - entry) * add_units
            current_realized = float(pos.realized_pnl) if pos.realized_pnl is not None else 0
            pos.realized_pnl = Decimal(str(round(current_realized + realized_this_tp, 2)))
            update_fields.append("realized_pnl")
        if pos.closed_units >= total_units:
            pos.status = Position.STATUS_CLOSED
            pos.closed_at = now
            try:
                tp_price_val = data.get(f"tp{next_tp}_price")
                if tp_price_val is not None:
                    pos.exit_price = float(str(tp_price_val).strip())
                else:
                    pos.exit_price = pos.entry_price
            except (TypeError, ValueError):
                pos.exit_price = pos.entry_price
            update_fields.extend(["status", "exit_price", "closed_at"])
        pos.save(update_fields=update_fields)
    else:
        pos.sl_hit = True
        pos.status = Position.STATUS_CLOSED
        pos.exit_price = pos.entry_price
        pos.closed_at = now
        pos.save(update_fields=["sl_hit", "status", "exit_price", "closed_at", "updated_at"])
    return True


@login_required
@require_GET
def position_management(request):
    """List open and closed positions for the current user. Paginated: 5 per page."""
    from django.utils import timezone
    open_qs = Position.objects.filter(user=request.user, status=Position.STATUS_OPEN).select_related("signal").order_by("-opened_at")
    closed_qs = Position.objects.filter(user=request.user, status=Position.STATUS_CLOSED).select_related("signal").order_by("-closed_at", "-opened_at")

    open_paginator = Paginator(open_qs, 5)
    open_page = open_paginator.get_page(request.GET.get("page", 1))
    closed_paginator = Paginator(closed_qs, 5)
    closed_page = closed_paginator.get_page(request.GET.get("closed_page", 1))

    open_positions = []
    for p in open_page.object_list:
        entry = float(p.entry_price) if p.entry_price is not None else 0
        qty = p.quantity * p.multiplier
        closed_u = p.closed_units or 0
        closed_pct = (100 * closed_u / qty) if qty else 0
        mark = _get_position_current_price(p)
        mark_val = float(mark) if mark is not None else None
        pnl_pct = (100 * (mark_val - entry) / entry) if entry and mark_val is not None else None
        realized = float(p.realized_pnl) if p.realized_pnl is not None else 0
        realized_pct = (100 * realized / (entry * qty)) if entry and qty else None
        next_tp = (p.tp_hit_level or 0) + 1
        _tp_embed = _build_position_update_embed(p, kind="tp", tp_level=next_tp, partial_exit=False) if (not p.sl_hit and next_tp) else {}
        _tp_embed_partial = _build_position_update_embed(p, kind="tp", tp_level=next_tp, partial_exit=True) if (not p.sl_hit and next_tp) else {}
        _sl_embed = _build_position_update_embed(p, kind="sl", tp_level=None, partial_exit=False) if not p.sl_hit else {}
        import json as _json
        # Keep description_after in preview JSON for modal; add disclaimer to match sent embeds
        preview_tp = _ensure_embed_disclaimer(dict(_tp_embed)) if _tp_embed else {}
        preview_tp_partial = _ensure_embed_disclaimer(dict(_tp_embed_partial)) if _tp_embed_partial else {}
        preview_sl = _ensure_embed_disclaimer(dict(_sl_embed)) if _sl_embed else {}
        
        # Extract current trade plan values for Edit Parameters modal
        data = (p.signal.data if p.signal and isinstance(getattr(p.signal, "data", None), dict) else {}) or {}
        current_takeoff_percent = ""
        next_target_percent = ""
        next_target_value = ""
        current_raise_sl_to = "off"
        current_raise_sl_custom_per = ""
        current_raise_sl_custom_price = ""
        current_raise_sl_custom_stock = ""
        if next_tp > 0:
            takeoff_raw = data.get(f"tp{next_tp}_takeoff_per")
            if takeoff_raw is not None:
                try:
                    current_takeoff_percent = str(takeoff_raw).strip().replace("%", "")
                except:
                    pass
            next_tp_per_raw = data.get(f"tp{next_tp}_per")
            if next_tp_per_raw is not None:
                try:
                    next_target_percent = str(next_tp_per_raw).strip().replace("%", "")
                except:
                    pass
            next_tp_price_raw = data.get(f"tp{next_tp}_price") or data.get(f"tp{next_tp}_stock_price")
            if next_tp_price_raw is not None:
                try:
                    next_target_value = str(next_tp_price_raw).strip()
                except:
                    pass
            raise_sl_raw = data.get(f"tp{next_tp}_raise_sl_to")
            if raise_sl_raw:
                current_raise_sl_to = str(raise_sl_raw).strip().lower()
                if current_raise_sl_to == "off":
                    current_raise_sl_to = "off"
                elif current_raise_sl_to == "entry" or current_raise_sl_to == "break_even":
                    current_raise_sl_to = "entry"
                elif current_raise_sl_to == "custom":
                    current_raise_sl_to = "custom"
                    current_raise_sl_custom_per = str(data.get(f"tp{next_tp}_raise_sl_custom_per") or "").strip()
                    current_raise_sl_custom_price = str(data.get(f"tp{next_tp}_raise_sl_custom") or "").strip()
                    current_raise_sl_custom_stock = str(data.get(f"tp{next_tp}_raise_sl_custom_stock") or "").strip()
        
        open_positions.append({
            "id": p.id,
            "symbol": p.symbol,
            "instrument": p.instrument,
            "option_type": p.option_type,
            "strike": p.strike,
            "expiration": p.expiration,
            "display_qty": qty,
            "closed_units": closed_u,
            "closed_pct": closed_pct,
            "entry_str": f"{entry:.2f}" if entry else "-",
            "mark_str": f"{mark_val:.2f}" if mark_val is not None else "-",
            "status_kind": "good" if (pnl_pct or 0) >= 0 else "bad",
            "status_label": "Opened" if not p.sl_hit and (p.tp_hit_level or 0) == 0 else (f"TP{p.tp_hit_level} Hit" if not p.sl_hit else "SL"),
            "pnl_pct": pnl_pct,
            "pnl_pct_str": f"{pnl_pct:+.1f}%" if pnl_pct is not None else "-",
            "realized_pnl_pct": realized_pct,
            "realized_pnl_pct_str": f"{realized_pct:+.1f}%" if realized_pct is not None else "-",
            "opened_at": p.opened_at,
            "mode": p.mode,
            "mode_label": "Automatic" if p.mode == Position.MODE_AUTO else "Manual",
            "preview_tp_embed": _json.dumps(preview_tp, ensure_ascii=False) if preview_tp else "",
            "preview_tp_partial_embed": _json.dumps(preview_tp_partial, ensure_ascii=False) if preview_tp_partial else "",
            "preview_sl_embed": _json.dumps(preview_sl, ensure_ascii=False) if preview_sl else "",
            "current_takeoff_percent": current_takeoff_percent,
            "next_target_percent": next_target_percent,
            "next_target_value": next_target_value,
            "current_raise_sl_to": current_raise_sl_to,
            "current_raise_sl_custom_per": current_raise_sl_custom_per,
            "current_raise_sl_custom_price": current_raise_sl_custom_price,
            "current_raise_sl_custom_stock": current_raise_sl_custom_stock,
        })
    closed_positions = []
    for p in closed_qs:
        entry = float(p.entry_price) if p.entry_price is not None else 0
        exit_p = float(p.exit_price) if p.exit_price is not None else 0
        qty = p.quantity * p.multiplier
        pnl_pct = (100 * (exit_p - entry) / entry) if entry and exit_p else None
        closed_positions.append({
            "id": p.id,
            "symbol": p.symbol,
            "instrument": p.instrument,
            "option_type": p.option_type,
            "strike": p.strike,
            "expiration": p.expiration,
            "display_qty": qty,
            "entry_str": f"{entry:.2f}" if entry else "-",
            "exit_str": f"{exit_p:.2f}" if exit_p else "-",
            "pnl_pct": pnl_pct,
            "pnl_pct_str": f"{pnl_pct:+.1f}%" if pnl_pct is not None else "-",
            "closed_at": p.closed_at,
        })
    return render(request, "signals/positions.html", {
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "open_page": open_page,
        "closed_page": closed_page,
    })


@login_required
@require_GET
def positions_live(request):
    """
    API: return current price (mark) and P/L for all open positions.
    Used by Position Management page for real-time updates.
    Bypasses cache to ensure fresh prices for live updates.
    """
    open_qs = Position.objects.filter(
        user=request.user,
        status=Position.STATUS_OPEN,
    ).select_related("signal").order_by("-opened_at")
    positions = []
    for p in open_qs:
        entry = float(p.entry_price) if p.entry_price is not None else 0
        qty = (p.quantity or 1) * (p.multiplier or 100)
        # Use bypass_cache=True for live updates to get fresh prices
        mark_val = _get_position_current_price(p, bypass_cache=True)
        pnl_pct = (100 * (mark_val - entry) / entry) if entry and mark_val is not None else None
        realized = float(p.realized_pnl) if p.realized_pnl is not None else 0
        realized_pct = (100 * realized / (entry * qty)) if entry and qty else None
        status_kind = "good" if (pnl_pct or 0) >= 0 else "bad"
        realized_kind = "good" if (realized_pct or 0) >= 0 else "bad"
        positions.append({
            "id": p.id,
            "mark_str": f"{mark_val:.2f}" if mark_val is not None else "-",
            "pnl_pct_str": f"{pnl_pct:+.1f}%" if pnl_pct is not None else "-",
            "status_kind": status_kind,
            "realized_pnl_pct_str": f"{realized_pct:+.1f}%" if realized_pct is not None else "-",
            "realized_kind": realized_kind,
        })
    return JsonResponse({"positions": positions})


@login_required
@require_GET
def leaderboard(request):
    """Leaderboard: wins/losses by user from closed positions."""
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import Count, Q, F, Avg, Case, When, Value, FloatField

    range_param = (request.GET.get("range") or "week").strip().lower()
    which = (request.GET.get("which") or "this").strip().lower()
    if which not in ("this", "last"):
        which = "this"
    period = "week" if range_param == "week" else "month"

    tz = timezone.get_current_timezone()
    now = timezone.now()
    if period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        while start.weekday() != 0:
            start = start - timedelta(days=1)
        if which == "last":
            start = start - timedelta(days=7)
        end = start + timedelta(days=7)
    else:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if which == "last":
            start = start - timedelta(days=32)
            start = start.replace(day=1)
        end = start + timedelta(days=32)
        end = end.replace(day=1)

    closed = Position.objects.filter(
        status=Position.STATUS_CLOSED,
        closed_at__gte=start,
        closed_at__lt=end,
        entry_price__isnull=False,
        exit_price__isnull=False,
    ).exclude(entry_price=0)

    # Per-user stats with avg P/L %
    user_stats = closed.values("user_id", "user__username").annotate(
        trades=Count("id"),
        wins=Count("id", filter=Q(exit_price__gt=F("entry_price"))),
        losses=Count("id", filter=Q(exit_price__lt=F("entry_price"))),
        avg_pnl_pct=Avg(
            Case(
                When(
                    entry_price__gt=0,
                    then=100 * (F("exit_price") - F("entry_price")) / F("entry_price"),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            )
        ),
    )

    rows = []
    for s in user_stats:
        u = s["user__username"] or "?"
        w = s["wins"] or 0
        l = s["losses"] or 0
        t = s["trades"] or 0
        avg_pnl = float(s["avg_pnl_pct"] or 0)
        try:
            user = User.objects.get(pk=s["user_id"])
            name = user.get_full_name().strip() or user.username
        except User.DoesNotExist:
            name = u
        rows.append({
            "username": u,
            "name": name,
            "trades": t,
            "wins": w,
            "losses": l,
            "win_rate": (100 * w / t) if t else 0,
            "avg_pnl_pct": avg_pnl,
        })
    rows.sort(key=lambda x: (-x["wins"], -x["trades"]))

    # Overall stats for the period
    overall_trades = closed.count()
    overall_wins = closed.filter(exit_price__gt=F("entry_price")).count()
    overall_losses = closed.filter(exit_price__lt=F("entry_price")).count()
    overall_avg_pnl = closed.aggregate(
        avg=Avg(
            Case(
                When(
                    entry_price__gt=0,
                    then=100 * (F("exit_price") - F("entry_price")) / F("entry_price"),
                ),
                default=Value(0.0),
                output_field=FloatField(),
            )
        )
    )["avg"] or 0
    overall = {
        "trades": overall_trades,
        "wins": overall_wins,
        "losses": overall_losses,
        "avg_pnl_pct": float(overall_avg_pnl),
        "win_rate": (100 * overall_wins / overall_trades) if overall_trades else 0,
    }

    # Display labels
    start_local = timezone.localtime(start, tz)
    end_local = timezone.localtime(end - timedelta(seconds=1), tz)
    period_title = f"This {period.capitalize()}" if which == "this" else f"Last {period.capitalize()}"
    date_range = f"{start_local.strftime('%b %d')} – {end_local.strftime('%b %d, %Y')}"

    return render(
        request,
        "signals/leaderboard.html",
        {
            "rows": rows,
            "period": period,
            "which": which,
            "period_title": period_title,
            "date_range": date_range,
            "overall": overall,
        },
    )


@login_required
@require_http_methods(["POST"])
def close_position(request, position_id):
    pos = Position.objects.filter(user=request.user, id=position_id).first()
    if not pos:
        return JsonResponse({"error": "Position not found"}, status=404)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    exit_price_raw = payload.get("exit_price")
    try:
        exit_price = float(str(exit_price_raw or "").strip()) if str(exit_price_raw or "").strip() else 0.0
    except Exception:
        exit_price = 0.0
    if exit_price <= 0:
        return JsonResponse({"error": "exit_price is required"}, status=400)
    from django.utils import timezone
    pos.status = Position.STATUS_CLOSED
    pos.exit_price = exit_price
    pos.closed_at = timezone.now()
    pos.save(update_fields=["status", "exit_price", "closed_at", "updated_at"])
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["POST"])
def set_position_mode(request, position_id):
    pos = Position.objects.filter(user=request.user, id=position_id).first()
    if not pos:
        return JsonResponse({"error": "Position not found"}, status=404)
    pos.mode = Position.MODE_MANUAL
    pos.tp_hit_level = pos.tp_hit_level or 0
    pos.sl_hit = bool(pos.sl_hit)
    pos.save(update_fields=["mode", "tp_hit_level", "sl_hit", "updated_at"])
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["POST"])
def post_position_update(request, position_id):
    pos = Position.objects.filter(user=request.user, id=position_id).first()
    if not pos:
        return JsonResponse({"error": "Position not found"}, status=404)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except Exception:
        payload = {}
    
    # Handle option contract update (for editing contract in position management)
    if "option_contract" in payload:
        contract = str(payload.get("option_contract") or "").strip()[:64]
        pos.option_contract = contract
        pos.save(update_fields=["option_contract"])
        return JsonResponse({"ok": True})
    
    # Handle parameter updates (Edit Parameters modal)
    if payload.get("update_parameters"):
        if not pos.signal:
            return JsonResponse({"error": "Position has no signal"}, status=400)
        data = pos.signal.data if isinstance(getattr(pos.signal, "data", None), dict) else {}
        if not isinstance(data, dict):
            data = {}
        next_tp = (pos.tp_hit_level or 0) + 1
        if next_tp > 0:
            reduce_percent = payload.get("reduce_percent")
            if reduce_percent is not None:
                data[f"tp{next_tp}_takeoff_per"] = str(reduce_percent).strip()
            next_target_percent = payload.get("next_target_percent")
            if next_target_percent is not None:
                # Next target is for TP{next_tp + 1}, not TP{next_tp}
                data[f"tp{next_tp + 1}_per"] = str(next_target_percent).strip()
            next_target_value = payload.get("next_target_value")
            if next_target_value is not None:
                # Try to determine if this is stock price or option price based on instrument
                is_shares = getattr(pos, "instrument", None) == Position.INSTRUMENT_SHARES
                # Next target is for TP{next_tp + 1}, not TP{next_tp}
                if is_shares:
                    data[f"tp{next_tp + 1}_stock_price"] = str(next_target_value).strip()
                else:
                    data[f"tp{next_tp + 1}_price"] = str(next_target_value).strip()
            raise_sl_to = payload.get("raise_sl_to")
            if raise_sl_to is not None:
                raise_sl_to_val = str(raise_sl_to).strip().lower()
                if raise_sl_to_val == "off":
                    # Remove raise_sl_to if it exists
                    data.pop(f"tp{next_tp}_raise_sl_to", None)
                else:
                    data[f"tp{next_tp}_raise_sl_to"] = raise_sl_to_val
                    if raise_sl_to_val == "custom":
                        raise_sl_custom_per = payload.get("raise_sl_custom_per")
                        if raise_sl_custom_per is not None:
                            data[f"tp{next_tp}_raise_sl_custom_per"] = str(raise_sl_custom_per).strip()
                        raise_sl_custom_price = payload.get("raise_sl_custom_price")
                        if raise_sl_custom_price is not None:
                            data[f"tp{next_tp}_raise_sl_custom"] = str(raise_sl_custom_price).strip()
                        raise_sl_custom_stock = payload.get("raise_sl_custom_stock")
                        if raise_sl_custom_stock is not None:
                            data[f"tp{next_tp}_raise_sl_custom_stock"] = str(raise_sl_custom_stock).strip()
                    else:
                        # Remove custom fields if not custom
                        data.pop(f"tp{next_tp}_raise_sl_custom_per", None)
                        data.pop(f"tp{next_tp}_raise_sl_custom", None)
                        data.pop(f"tp{next_tp}_raise_sl_custom_stock", None)
        pos.signal.data = data
        pos.signal.save(update_fields=["data"])
        return JsonResponse({"ok": True})
    
    # Handle exit updates (TP/SL)
    kind = (payload.get("kind") or "tp").strip().lower()
    if kind not in ("tp", "sl"):
        return JsonResponse({"error": "kind must be tp or sl"}, status=400)
    partial_exit = payload.get("partial_exit") in (True, "true", "1", 1)
    
    # For partial exit, save parameters to signal.data before building embed (so Discord message uses latest values)
    if partial_exit and pos.signal:
        data = pos.signal.data if isinstance(getattr(pos.signal, "data", None), dict) else {}
        if not isinstance(data, dict):
            data = {}
        next_tp = (pos.tp_hit_level or 0) + 1
        if next_tp > 0:
            reduce_percent = payload.get("reduce_percent")
            if reduce_percent is not None:
                data[f"tp{next_tp}_takeoff_per"] = str(reduce_percent).strip()
            next_target_percent = payload.get("next_target_percent")
            if next_target_percent is not None:
                data[f"tp{next_tp + 1}_per"] = str(next_target_percent).strip()
            next_target_value = payload.get("next_target_value")
            if next_target_value is not None:
                is_shares = getattr(pos, "instrument", None) == Position.INSTRUMENT_SHARES
                if is_shares:
                    data[f"tp{next_tp + 1}_stock_price"] = str(next_target_value).strip()
                else:
                    data[f"tp{next_tp + 1}_price"] = str(next_target_value).strip()
            raise_sl_to = payload.get("raise_sl_to")
            if raise_sl_to is not None:
                raise_sl_to_val = str(raise_sl_to).strip().lower()
                if raise_sl_to_val == "off":
                    data.pop(f"tp{next_tp}_raise_sl_to", None)
                else:
                    data[f"tp{next_tp}_raise_sl_to"] = raise_sl_to_val
                    if raise_sl_to_val == "custom":
                        raise_sl_custom_per = payload.get("raise_sl_custom_per")
                        if raise_sl_custom_per is not None:
                            data[f"tp{next_tp}_raise_sl_custom_per"] = str(raise_sl_custom_per).strip()
                        raise_sl_custom_price = payload.get("raise_sl_custom_price")
                        if raise_sl_custom_price is not None:
                            data[f"tp{next_tp}_raise_sl_custom"] = str(raise_sl_custom_price).strip()
                        raise_sl_custom_stock = payload.get("raise_sl_custom_stock")
                        if raise_sl_custom_stock is not None:
                            data[f"tp{next_tp}_raise_sl_custom_stock"] = str(raise_sl_custom_stock).strip()
                    else:
                        data.pop(f"tp{next_tp}_raise_sl_custom_per", None)
                        data.pop(f"tp{next_tp}_raise_sl_custom", None)
                        data.pop(f"tp{next_tp}_raise_sl_custom_stock", None)
        pos.signal.data = data
        pos.signal.save(update_fields=["data"])
    
    current_price = payload.get("current_price")
    next_steps = payload.get("next_steps")
    if next_steps is not None and not isinstance(next_steps, str):
        next_steps = str(next_steps) if next_steps else ""
    risk_management = payload.get("risk_management")
    if risk_management is not None and not isinstance(risk_management, str):
        risk_management = str(risk_management) if risk_management else ""
    strategy_executed = payload.get("strategy_executed")
    if strategy_executed is not None and not isinstance(strategy_executed, str):
        strategy_executed = str(strategy_executed) if strategy_executed else ""
    if not _apply_position_exit(pos, kind, current_price=current_price, next_steps=next_steps, risk_management=risk_management, strategy_executed=strategy_executed, partial_exit=partial_exit):
        return JsonResponse({"error": "Failed to send to Discord"}, status=500)
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["GET"])
def position_preview(request, position_id):
    """
    GET: Return the Discord embed JSON for a position (for Partial or Full exit preview).
    Query params: partial=1 for partial exit, current_price= optional override.
    Uses current signal.data so after Edit Parameters save, refetch to get updated preview.
    If query param include_params=1, also returns current parameter values.
    """
    pos = Position.objects.filter(user=request.user, id=position_id).select_related("signal").first()
    if not pos:
        return JsonResponse({"error": "Position not found"}, status=404)
    partial = request.GET.get("partial", "").strip().lower() in ("1", "true", "yes")
    include_params = request.GET.get("include_params", "").strip().lower() in ("1", "true", "yes")
    current_price = request.GET.get("current_price")
    override_price = None
    if current_price is not None:
        try:
            override_price = float(str(current_price).strip())
        except (TypeError, ValueError):
            pass
    next_tp = (pos.tp_hit_level or 0) + 1
    if partial:
        embed = _build_position_update_embed(
            pos, kind="tp", tp_level=next_tp, override_price=override_price, partial_exit=True
        ) if (not pos.sl_hit and next_tp) else {}
    else:
        kind = (request.GET.get("kind") or "tp").strip().lower()
        if kind not in ("tp", "sl"):
            kind = "tp"
        embed = _build_position_update_embed(
            pos, kind=kind, tp_level=next_tp if kind == "tp" else None,
            override_price=override_price, partial_exit=False
        )
    embed = _ensure_embed_disclaimer(dict(embed)) if embed else {}
    
    if include_params and next_tp > 0:
        data = (pos.signal.data if pos.signal and isinstance(getattr(pos.signal, "data", None), dict) else {}) or {}
        params = {}
        takeoff_raw = data.get(f"tp{next_tp}_takeoff_per")
        if takeoff_raw is not None:
            try:
                params["reduce_percent"] = str(takeoff_raw).strip().replace("%", "")
            except:
                pass
        # Next target is for TP{next_tp + 1}, not TP{next_tp}
        next_tp_per_raw = data.get(f"tp{next_tp + 1}_per")
        if next_tp_per_raw is not None:
            try:
                params["next_target_percent"] = str(next_tp_per_raw).strip().replace("%", "")
            except:
                pass
        next_tp_price_raw = data.get(f"tp{next_tp + 1}_price") or data.get(f"tp{next_tp + 1}_stock_price")
        if next_tp_price_raw is not None:
            try:
                params["next_target_value"] = str(next_tp_price_raw).strip()
            except:
                pass
        # Get TP mode to determine which custom inputs to show (for next TP level)
        tp_mode_raw = str(data.get(f"tp{next_tp + 1}_mode") or "").strip().lower()
        is_stock_mode = tp_mode_raw in ("stock", "stock_price", "underlying", "share_price")
        params["tp_mode"] = "stock" if is_stock_mode else "percent"
        raise_sl_raw = data.get(f"tp{next_tp}_raise_sl_to")
        if raise_sl_raw:
            raise_sl_val = str(raise_sl_raw).strip().lower()
            if raise_sl_val == "off":
                params["raise_sl_to"] = "off"
            elif raise_sl_val == "entry" or raise_sl_val == "break_even":
                params["raise_sl_to"] = "entry"
            elif raise_sl_val == "custom":
                params["raise_sl_to"] = "custom"
                params["raise_sl_custom_per"] = str(data.get(f"tp{next_tp}_raise_sl_custom_per") or "").strip()
                params["raise_sl_custom_price"] = str(data.get(f"tp{next_tp}_raise_sl_custom") or "").strip()
                params["raise_sl_custom_stock"] = str(data.get(f"tp{next_tp}_raise_sl_custom_stock") or "").strip()
        embed["_params"] = params
    
    return JsonResponse(embed)


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
    include_crypto = True  # include crypto tickers in search results

    if source == "tradingview":
        # Live search only (TradingView endpoint is search-oriented, not a full-universe dump).
        tickers = []
        try:
            stock_tickers = _search_tickers_tradingview(q, limit=limit or 40, include_etfs=include_etfs) if q else []
            tickers.extend(stock_tickers)
        except Exception as e:
            # Fall back to cache to keep UI usable.
            # (We intentionally don't expose internal error details to the client.)
            pass
        
        # Add crypto tickers if query provided and crypto is enabled
        if q and include_crypto:
            try:
                crypto_limit = max(10, (limit or 40) - len(tickers))  # Reserve some slots for crypto
                crypto_tickers = _search_crypto_tickers_polygon(q, limit=crypto_limit)
                tickers.extend(crypto_tickers)
            except Exception as e:
                logger.warning(f"Crypto ticker search failed: {e}")
        
        if tickers:
            # Sort: exact symbol matches first, then prefix matches, then alphabetical
            q_upper = q.upper()
            tickers.sort(key=lambda r: (
                0 if r["symbol"].upper() == q_upper else (1 if r["symbol"].upper().startswith(q_upper) else 2),
                r["symbol"]
            ))
            # Apply limit after combining
            if limit:
                tickers = tickers[:limit]
            return JsonResponse({"tickers": tickers, "source": "tradingview"})
        
        # Continue into cache flow below if no results.

    # Cache flow (fallback or explicit): ensure popular ETFs (SPY, QQQ, etc.) are always in the list
    POPULAR_ETFS = [
        {"symbol": "SPY", "name": "SPDR S&P 500 ETF Trust"},
        {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
        {"symbol": "IWM", "name": "iShares Russell 2000 ETF"},
        {"symbol": "DIA", "name": "SPDR Dow Jones Industrial Average ETF"},
        {"symbol": "VOO", "name": "Vanguard S&P 500 ETF"},
    ]
    # Popular crypto symbols (for cache flow when searching) - use USD suffix format
    POPULAR_CRYPTOS = [
        {"symbol": "BTCUSD", "name": "Bitcoin"},
        {"symbol": "ETHUSD", "name": "Ethereum"},
        {"symbol": "SOLUSD", "name": "Solana"},
        {"symbol": "ADAUSD", "name": "Cardano"},
        {"symbol": "XRPUSD", "name": "Ripple"},
        {"symbol": "DOGEUSD", "name": "Dogecoin"},
        {"symbol": "MATICUSD", "name": "Polygon"},
        {"symbol": "AVAXUSD", "name": "Avalanche"},
        {"symbol": "LINKUSD", "name": "Chainlink"},
        {"symbol": "UNIUSD", "name": "Uniswap"},
    ]
    tickers_all = list(get_us_tickers() or [])
    seen = {str(t.get("symbol") or "").strip().upper() for t in tickers_all if isinstance(t, dict)}
    for etf in POPULAR_ETFS:
        sym = str(etf.get("symbol") or "").strip().upper()
        if sym and sym not in seen:
            tickers_all.append(etf)
            seen.add(sym)
    
    # Add popular cryptos to cache if searching (they'll be filtered by query below)
    if q and include_crypto:
        for crypto in POPULAR_CRYPTOS:
            sym = str(crypto.get("symbol") or "").strip().upper()
            if sym and sym not in seen:
                tickers_all.append(crypto)
                seen.add(sym)
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

    # Quote unavailable: return 200 with price=null so the UI can show "Unavailable" instead of "Bad Gateway".
    # polygon_error (e.g. http 403 = options not in plan) is included for debugging.
    polygon_err = getattr(client, "last_error", None)
    logger.warning(
        "Option quote unavailable: contract=%s polygon_error=%s",
        contract,
        polygon_err,
    )
    payload = {
        "symbol": symbol,
        "expiration": expiration,
        "strike": strike,
        "side": side,
        "contract": contract,
        "price": None,
        "error": "option quote failed",
        "source": "polygon",
        "polygon_error": polygon_err,
    }
    return JsonResponse(payload)


@login_required
@require_GET
def best_option(request):
    """
    Pick the best option contract for an underlying according to trade_type rules.

    Query params:
      - symbol: underlying ticker (e.g. AAPL)
      - trade_type: Scalp|Swing|Leap
      - side: call|put (optional; defaults to call)
      - stock_price: optional; if provided and valid, used as underlying price instead of fetching from Polygon

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

    # Use client-provided stock_price if valid; otherwise fetch from Polygon
    stock_price_param = request.GET.get("stock_price") or request.GET.get("underlying_price")
    try:
        underlying_price = float(stock_price_param) if stock_price_param else None
    except (TypeError, ValueError):
        underlying_price = None
    if underlying_price is None or underlying_price <= 0:
        client = PolygonClient(polygon_key)
        underlying_price = client.get_share_current_price(symbol)
        if underlying_price is None:
            payload = {"error": "underlying price unavailable", "source": "polygon"}
            if getattr(settings, "DEBUG", False):
                payload["polygon_error"] = getattr(client, "last_error", None)
            return JsonResponse(payload, status=502)
    else:
        client = PolygonClient(polygon_key)

    today = dt.date.today()
    # Fetch wider DTE ranges to cover all fallback levels
    # The selection logic will filter by level-specific criteria
    if trade_type == "scalp":
        lo, hi = 0, 2  # Covers all scalp levels (0-2 DTE)
    elif trade_type == "leap":
        lo, hi = 120, 550  # Covers all leap levels (120-550 DTE)
    else:  # swing
        lo, hi = 6, 90  # Covers all swing levels (6-90 DTE)

    exp_gte = (today + dt.timedelta(days=lo)).isoformat()
    exp_lte = (today + dt.timedelta(days=hi)).isoformat()

    # Strike filter: optional request params strike_gte / strike_lte; else default 0.5x–2x underlying price
    strike_gte = request.GET.get("strike_gte")
    strike_lte = request.GET.get("strike_lte")
    try:
        strike_gte = float(strike_gte) if strike_gte not in (None, "") else None
    except (TypeError, ValueError):
        strike_gte = None
    try:
        strike_lte = float(strike_lte) if strike_lte not in (None, "") else None
    except (TypeError, ValueError):
        strike_lte = None
    if strike_gte is None or strike_lte is None:
        try:
            price_float = float(underlying_price)
            if strike_gte is None:
                strike_gte = round(price_float * 0.5, 2)
            if strike_lte is None:
                strike_lte = round(price_float * 2.0, 2)
        except (TypeError, ValueError):
            pass

    # Fetch option chain (with pagination + filter) and pick best contract in one call
    best, fetch_failed = client.get_best_option(
        underlying=symbol,
        side=side,
        expiration_gte=exp_gte,
        expiration_lte=exp_lte,
        strike_gte=strike_gte,
        strike_lte=strike_lte,
        underlying_price=float(underlying_price),
        trade_type=trade_type,
        timeout=30,
    )
    if fetch_failed:
        payload = {"error": "options unavailable", "source": "polygon"}
        if getattr(settings, "DEBUG", False):
            payload["polygon_error"] = getattr(client, "last_error", None)
            payload["query"] = {"expiration_gte": exp_gte, "expiration_lte": exp_lte, "side": side, "trade_type": trade_type}
        return JsonResponse(payload, status=502)
    if not best:
        return JsonResponse(
            {
                "error": "No suitable option contract found after all fallback levels",
                "source": "polygon",
                "symbol": symbol,
                "underlying_price": underlying_price,
                "side": side,
                "trade_type": trade_type,
                "message": f"No viable contract found for {symbol} after trying all fallback levels (0-3). Please use Manual Option Contract selection.",
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

