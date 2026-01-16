from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.contrib.auth.forms import PasswordChangeForm
from django.conf import settings
from django.http import JsonResponse
from django.db.models import Q
from django.core.paginator import Paginator
from django.core.exceptions import ValidationError
import requests
import re
import json
from .forms import SignalForm, SignalTypeForm
from .models import Signal, SignalType, UserProfile, DiscordChannel

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

def render_fields_template(fields_template, variables, optional_fields_indices=None):
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
            "name": render_template(field.get('name', ''), variables),
            "value": render_template(field.get('value', ''), variables),
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
    title = render_template(signal_type.title_template, data_copy) if show_title else None
    description = render_template(signal_type.description_template, data_copy) if show_description else None
    footer = render_template(signal_type.footer_template, data_copy)
    
    # Render fields with optional field filtering
    fields = render_fields_template(signal_type.fileds_template, data_copy, optional_fields_indices)
    
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
    
    return render(request, 'signals/dashboard.html', {
        'form': form,
        'recent_signals': recent_signals,
        'signal_types_data': signal_types_data,
        'discord_channels': discord_channels
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
    })

@login_required
def signal_type_create(request):
    """Create a new signal type"""
    if request.method == 'POST':
        form = SignalTypeForm(request.POST, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Signal type created successfully!')
            return redirect('signal_types_list')
    else:
        form = SignalTypeForm(user=request.user)
    
    return render(request, 'signals/signal_type_form.html', {
        'form': form,
        'form_type': 'create'
    })

@login_required
def signal_type_edit(request, signal_type_id):
    """Edit an existing signal type"""
    signal_type = get_object_or_404(SignalType, id=signal_type_id)
    
    # Check if this is a system default template (user is None)
    is_default = signal_type.user is None
    
    # Check if user has permission to edit this signal type
    if signal_type.user and signal_type.user != request.user:
        messages.error(request, 'You do not have permission to edit this signal type.')
        return redirect('signal_types_list')
    
    # Prevent editing default templates
    if is_default and request.method == 'POST':
        messages.warning(request, 'System default templates are read-only and cannot be modified.')
        return redirect('signal_types_list')
    
    if request.method == 'POST':
        form = SignalTypeForm(request.POST, instance=signal_type, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, 'Signal type updated successfully!')
            return redirect('signal_types_list')
    else:
        form = SignalTypeForm(instance=signal_type, user=request.user)
    
    return render(request, 'signals/signal_type_form.html', {
        'form': form,
        'form_type': 'edit',
        'is_default': is_default,
        'signal_type': signal_type
    })

@login_required
def signal_type_delete(request, signal_type_id):
    """Delete a signal type"""
    signal_type = get_object_or_404(SignalType, id=signal_type_id)
    
    # Check if user has permission to delete this signal type
    if signal_type.user and signal_type.user != request.user:
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

