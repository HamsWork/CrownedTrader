from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.conf import settings
from django.http import JsonResponse
from django.db.models import Q
from django.core.paginator import Paginator
import requests
import re
import json
from .forms import SignalForm, SignalTypeForm
from .models import Signal, SignalType, UserProfile

def send_to_discord(signal):
    """Send signal data to Discord channel using user's webhook"""
    
    # Get the appropriate template based on signal type
    embed = get_signal_template(signal)
    
    payload = {
        "embeds": [embed]
    }
    
    # Try to get user's webhook first
    try:
        user_profile = signal.user.profile
        if user_profile and user_profile.discord_channel_webhook:
            url = user_profile.discord_channel_webhook
            headers = {
                "Content-Type": "application/json"
            }
        else:
            print(f"ERROR: User {signal.user.username} does not have a Discord webhook configured")
            return False
    except UserProfile.DoesNotExist:
        print(f"ERROR: User {signal.user.username} does not have a profile")
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

    
    while rendered_fields and not (rendered_fields[-1]["name"].strip()):
        rendered_fields.pop()
    
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
            
            # Create signal instance
            signal = Signal.objects.create(
                user=request.user,
                signal_type=signal_type,
                data=signal_data
            )
            
            # Send to Discord
            success = send_to_discord(signal)
            
            if success:
                messages.success(request, 'Signal submitted and sent to Discord successfully!')
            else:
                messages.warning(request, 'Signal saved but failed to send to Discord. Please ensure your user profile has a Discord webhook configured.')
            
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

def is_superuser(user):
    """Check if user is superuser"""
    return user.is_superuser

@login_required
@user_passes_test(is_superuser)
def user_management(request):
    """User management admin panel - list all users"""
    search_query = request.GET.get('search', '')
    users = User.objects.select_related('profile').all().order_by('-date_joined')
    
    if search_query:
        users = users.filter(
            Q(username__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(profile__discord_channel_name__icontains=search_query)
        )
    
    # Paginate users
    paginator = Paginator(users, 20)  # Show 20 users per page
    page_number = request.GET.get('page', 1)
    page_obj = paginator.get_page(page_number)
    
    return render(request, 'signals/user_management.html', {
        'users': page_obj,
        'search_query': search_query,
        'page_obj': page_obj
    })

@login_required
@user_passes_test(is_superuser)
def user_create(request):
    """Create a new user"""
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '').strip()
        discord_channel_name = request.POST.get('discord_channel_name', '').strip()
        discord_channel_webhook = request.POST.get('discord_channel_webhook', '').strip()
        is_superuser_check = request.POST.get('is_superuser') == 'on'
        
        # Validate required fields
        errors = []
        if not username:
            errors.append('Username is required.')
        if not email:
            errors.append('Email is required.')
        if not password:
            errors.append('Password is required.')
        if not discord_channel_name:
            errors.append('Discord Channel Name is required.')
        if not discord_channel_webhook:
            errors.append('Discord Channel Webhook is required.')
        
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'signals/user_form.html', {
                'form_type': 'create',
                'username': username,
                'email': email,
                'discord_channel_name': discord_channel_name,
                'discord_channel_webhook': discord_channel_webhook,
                'is_superuser': is_superuser_check
            })
        
        # Validate username uniqueness
        if User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
            return render(request, 'signals/user_form.html', {
                'form_type': 'create',
                'username': username,
                'email': email,
                'discord_channel_name': discord_channel_name,
                'discord_channel_webhook': discord_channel_webhook,
                'is_superuser': is_superuser_check
            })
        
        # Create user and profile
        try:
            user = User.objects.create_user(
                username=username,
                email=email,
                password=password,
                is_superuser=is_superuser_check,
                is_staff=is_superuser_check  # Staff status follows superuser status
            )
            # Create user profile with Discord information
            UserProfile.objects.create(
                user=user,
                discord_channel_name=discord_channel_name,
                discord_channel_webhook=discord_channel_webhook
            )
            messages.success(request, f'User "{username}" created successfully!')
            return redirect('user_management')
        except Exception as e:
            messages.error(request, f'Error creating user: {str(e)}')
    
    return render(request, 'signals/user_form.html', {'form_type': 'create'})

@login_required
@user_passes_test(is_superuser)
def user_edit(request, user_id):
    """Edit an existing user"""
    user = get_object_or_404(User, id=user_id)
    
    # Prevent editing superuser by non-superusers (extra safety)
    if not request.user.is_superuser:
        messages.error(request, 'You do not have permission to edit users.')
        return redirect('user_management')
    
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        discord_channel_name = request.POST.get('discord_channel_name', '').strip()
        discord_channel_webhook = request.POST.get('discord_channel_webhook', '').strip()
        is_superuser_check = request.POST.get('is_superuser') == 'on'
        is_active = request.POST.get('is_active') == 'on'
        new_password = request.POST.get('password', '').strip()
        
        # Validate required fields
        errors = []
        if not username:
            errors.append('Username is required.')
        if not email:
            errors.append('Email is required.')
        if not discord_channel_name:
            errors.append('Discord Channel Name is required.')
        if not discord_channel_webhook:
            errors.append('Discord Channel Webhook is required.')
        
        if errors:
            for error in errors:
                messages.error(request, error)
            # Get or create profile for rendering
            profile, _ = UserProfile.objects.get_or_create(user=user)
            return render(request, 'signals/user_form.html', {
                'form_type': 'edit',
                'user': user,
                'username': username,
                'email': email,
                'discord_channel_name': discord_channel_name,
                'discord_channel_webhook': discord_channel_webhook,
                'is_superuser': is_superuser_check,
                'is_active': is_active
            })
        
        # Check if username already exists (for other users)
        if username != user.username and User.objects.filter(username=username).exists():
            messages.error(request, 'Username already exists.')
            profile, _ = UserProfile.objects.get_or_create(user=user)
            return render(request, 'signals/user_form.html', {
                'form_type': 'edit',
                'user': user,
                'username': username,
                'email': email,
                'discord_channel_name': discord_channel_name,
                'discord_channel_webhook': discord_channel_webhook,
                'is_superuser': is_superuser_check,
                'is_active': is_active
            })
        
        # Update user
        user.username = username
        user.email = email
        user.is_superuser = is_superuser_check
        user.is_staff = is_superuser_check  # Staff status follows superuser status
        user.is_active = is_active
        
        # Update password if provided
        if new_password:
            user.set_password(new_password)
        
        user.save()
        
        # Update or create user profile
        profile, created = UserProfile.objects.get_or_create(user=user)
        profile.discord_channel_name = discord_channel_name
        profile.discord_channel_webhook = discord_channel_webhook
        profile.save()
        
        messages.success(request, f'User "{username}" updated successfully!')
        return redirect('user_management')
    
    # Get or create profile for display
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return render(request, 'signals/user_form.html', {
        'form_type': 'edit',
        'user': user,
        'discord_channel_name': profile.discord_channel_name,
        'discord_channel_webhook': profile.discord_channel_webhook
    })

@login_required
@user_passes_test(is_superuser)
def user_delete(request, user_id):
    """Delete a user"""
    user = get_object_or_404(User, id=user_id)
    
    # Prevent self-deletion
    if user.id == request.user.id:
        messages.error(request, 'You cannot delete your own account.')
        return redirect('user_management')
    
    username = user.username
    user.delete()
    messages.success(request, f'User "{username}" deleted successfully!')
    return redirect('user_management')


# Signal Type Builder Views
@login_required
def signal_types_list(request):
    """List all signal types for the current user (system defaults + user's custom types)"""
    user_signal_types = SignalType.objects.filter(user=request.user).order_by('-created_at')
    system_signal_types = SignalType.objects.filter(user__isnull=True).order_by('name')
    
    return render(request, 'signals/signal_types_list.html', {
        'user_signal_types': user_signal_types,
        'system_signal_types': system_signal_types,
    })


@login_required
def signal_type_create(request):
    """Create a new signal type"""
    if request.method == 'POST':
        form = SignalTypeForm(request.POST, user=request.user)
        if form.is_valid():
            signal_type = form.save()
            messages.success(request, f'Signal type "{signal_type.name}" created successfully!')
            return redirect('signal_types_list')
    else:
        form = SignalTypeForm(user=request.user)
    
    return render(request, 'signals/signal_type_form.html', {
        'form': form,
        'form_type': 'create',
    })


@login_required
def signal_type_edit(request, signal_type_id):
    """Edit an existing signal type"""
    signal_type = get_object_or_404(SignalType, id=signal_type_id, user=request.user)
    
    if request.method == 'POST':
        form = SignalTypeForm(request.POST, instance=signal_type, user=request.user)
        if form.is_valid():
            signal_type = form.save()
            messages.success(request, f'Signal type "{signal_type.name}" updated successfully!')
            return redirect('signal_types_list')
    else:
        form = SignalTypeForm(instance=signal_type, user=request.user)
    
    return render(request, 'signals/signal_type_form.html', {
        'form': form,
        'form_type': 'edit',
        'signal_type': signal_type,
    })


@login_required
def signal_type_delete(request, signal_type_id):
    """Delete a signal type"""
    signal_type = get_object_or_404(SignalType, id=signal_type_id, user=request.user)
    
    # Check if signal type is being used
    signals_count = signal_type.signals.count()
    if signals_count > 0:
        messages.error(request, f'Cannot delete signal type "{signal_type.name}" because it is being used by {signals_count} signal(s).')
        return redirect('signal_types_list')
    
    name = signal_type.name
    signal_type.delete()
    messages.success(request, f'Signal type "{name}" deleted successfully!')
    return redirect('signal_types_list')

