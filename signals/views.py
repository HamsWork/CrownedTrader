from django.shortcuts import render, redirect
from django.contrib import messages
from django.conf import settings
from django.http import JsonResponse
import requests
import re
import json
from .forms import SignalForm
from .models import Signal, SignalType

def send_to_discord(signal):
    """Send signal data to Discord channel using webhook or bot token"""
    
    # Get the appropriate template based on signal type
    embed = get_signal_template(signal)
    
    payload = {
        "embeds": [embed]
    }
    
    # Try webhook first (simpler, no permissions needed)
    if settings.DISCORD_WEBHOOK_URL:
        url = settings.DISCORD_WEBHOOK_URL
        headers = {
            "Content-Type": "application/json"
        }
    # Fallback to bot token method
    elif settings.DISCORD_BOT_TOKEN and settings.DISCORD_CHANNEL_ID:
        url = f"https://discord.com/api/v10/channels/{settings.DISCORD_CHANNEL_ID}/messages"
        headers = {
            "Authorization": f"Bot {settings.DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json"
        }
    else:
        print("ERROR: No Discord configuration found. Please set DISCORD_WEBHOOK_URL or DISCORD_BOT_TOKEN in .env")
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
            print("ERROR: Invalid bot token. Check your DISCORD_BOT_TOKEN in .env")
        elif response.status_code == 403:
            print("ERROR: Bot lacks permissions or isn't in the channel.")
            print("Solution: Make sure the bot is invited to the server and has 'Send Messages' permission")
        elif response.status_code == 404:
            print("ERROR: Channel not found. Check your DISCORD_CHANNEL_ID or webhook URL")
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

def render_template(template_string, variables):
    """Render template string by replacing {{variable}} placeholders with actual values"""
    if not template_string:
        return ""
    
    def replace_var(match):
        var_name = match.group(1).strip()
        return str(variables.get(var_name, ""))
    
    # Replace {{variable}} patterns
    result = re.sub(r'\{\{(\w+)\}\}', replace_var, template_string)
    return result

def render_fields_template(fields_template, variables):
    """Render fields template from JSONField"""
    if not fields_template:
        return []
    
    rendered_fields = []
    for field in fields_template:
        rendered_field = {
            "name": render_template(field.get('name', ''), variables),
            "value": render_template(field.get('value', ''), variables),
            "inline": field.get('inline', False)
        }
        rendered_fields.append(rendered_field)
    
    return rendered_fields

def get_signal_template(signal):
    """Return Discord embed template based on signal type from database"""
    
    signal_type = signal.signal_type
    
    # Build variables dictionary from signal.data - dynamically get all fields
    variables = signal.data.copy() if signal.data else {}
    
    # Render the embed using the template
    embed = {
        "title": render_template(signal_type.title_template, variables),
        "description": render_template(signal_type.description_template, variables),
        "color": hex_to_int(signal_type.color),
        "fields": render_fields_template(signal_type.fileds_template, variables),
        "footer": {
            "text": render_template(signal_type.footer_template, variables)
        }
    }
    
    return embed

def dashboard(request):
    if request.method == 'POST':
        form = SignalForm(request.POST)
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
            
            # Create signal instance
            signal = Signal.objects.create(
                signal_type=signal_type,
                data=signal_data
            )
            
            # Send to Discord
            success = send_to_discord(signal)
            
            if success:
                messages.success(request, 'Signal submitted and sent to Discord successfully!')
            else:
                messages.warning(request, 'Signal saved but failed to send to Discord. Check bot configuration.')
            
            return redirect('dashboard')
    else:
        form = SignalForm()
    
    # Get recent signals for display
    recent_signals = Signal.objects.all()[:10]
    
    # Get all signal types for JavaScript with serialized variables
    signal_types = SignalType.objects.all()
    signal_types_data = []
    for st in signal_types:
        signal_types_data.append({
            'id': st.id,
            'name': st.name,
            'variables': json.dumps(st.variables) if st.variables else '[]'
        })
    
    return render(request, 'signals/dashboard.html', {
        'form': form,
        'recent_signals': recent_signals,
        'signal_types': signal_types,
        'signal_types_data': signal_types_data
    })

def signals_history(request):
    """View all submitted signals"""
    signals = Signal.objects.all()
    
    # Filter by signal type if provided
    signal_type = request.GET.get('type')
    if signal_type:
        signals = signals.filter(signal_type=signal_type)
    
    return render(request, 'signals/history.html', {
        'signals': signals,
        'filter_type': signal_type
    })

def get_signal_type_variables(request):
    """API endpoint to get variables for a specific signal type"""
    signal_type_id = request.GET.get('signal_type_id')
    
    if not signal_type_id:
        return JsonResponse({'error': 'No signal_type_id provided'}, status=400)
    
    try:
        signal_type = SignalType.objects.get(id=signal_type_id)
        return JsonResponse({
            'variables': signal_type.variables,
            'title_template': signal_type.title_template,
            'description_template': signal_type.description_template,
            'color': signal_type.color
        })
    except SignalType.DoesNotExist:
        return JsonResponse({'error': 'Signal type not found'}, status=404)

