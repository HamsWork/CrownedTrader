from django.shortcuts import render, redirect
from django.contrib import messages
from django.conf import settings
import requests
from .forms import SignalForm
from .models import Signal

def send_to_discord(ticker, contract_info, signal_type, extra_info):
    """Send signal data to Discord webhook"""
    if not settings.DISCORD_WEBHOOK_URL:
        return False
    
    # Format the message for Discord
    signal_type_display = dict(Signal.SignalType.choices)[signal_type]
    
    embed = {
        "title": "🚨 New Trading Signal",
        "description": f"**Ticker:** {ticker}\n**Signal Type:** {signal_type_display}\n\n**Contract Information:**\n{contract_info}",
        "color": get_color_for_signal(signal_type),
        "fields": [
            {
                "name": "Extra Information",
                "value": extra_info if extra_info else "None provided",
                "inline": False
            }
        ],
        "timestamp": None
    }
    
    payload = {
        "embeds": [embed]
    }
    
    try:
        response = requests.post(settings.DISCORD_WEBHOOK_URL, json=payload)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        print(f"Failed to send to Discord: {e}")
        return False

def get_color_for_signal(signal_type):
    """Return color code based on signal type"""
    colors = {
        'entry': 0x00ff00,  # Green for Entry
        'stop_loss': 0xff0000,  # Red for Stop Loss
        'take_profit': 0x00ffff,  # Cyan for Take Profit
    }
    return colors.get(signal_type, 0x808080)  # Default gray

def dashboard(request):
    if request.method == 'POST':
        form = SignalForm(request.POST)
        if form.is_valid():
            signal = form.save()
            
            # Send to Discord
            success = send_to_discord(
                signal.ticker,
                signal.contract_info,
                signal.signal_type,
                signal.extra_info
            )
            
            if success:
                messages.success(request, 'Signal submitted and sent to Discord successfully!')
            else:
                messages.warning(request, 'Signal saved but failed to send to Discord. Check webhook configuration.')
            
            return redirect('dashboard')
    else:
        form = SignalForm()
    
    # Get recent signals for display
    recent_signals = Signal.objects.all()[:10]
    
    return render(request, 'signals/dashboard.html', {
        'form': form,
        'recent_signals': recent_signals
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

