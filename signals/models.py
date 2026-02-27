from django.db import models
from django.contrib.auth.models import User

class SignalType(models.Model):
    name = models.CharField(max_length=50, help_text="Singal type name, e.g. Entry, Stop Loss, Take Profit")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='signal_types', null=True, blank=True, help_text="User who owns this signal type. Null for system defaults.")
    
    variables = models.JSONField(default=list, 
                                 help_text="List of variables for the signal type, e.g. [{'name': 'ticker', 'type': 'string'}, {'name': 'strike', 'type': 'number'}, {'name': 'expiration', 'type': 'date'}]")
    
    title_template = models.TextField(
        help_text="Template for the title of the signal e.g. '{ticker} Trade Alert'"
    )
    
    description_template = models.TextField(
        help_text="Template for the description of the signal e.g. '{{ticker}} is trading at {{strike}} and expires on {{expiration}}' will be replaced with the ticker, strike, and expiration values"
    )
    
    color = models.CharField(max_length=7, default="#000000", help_text="Color code for the signal type, e.g. #000000 for black")
    
    fileds_template = models.JSONField(
        default=list, 
        help_text="List of fields for the signal embed, e.g. [{'name': 'Ticker', 'value': '{{ticker}}'}, {'name': 'Strike', 'value': '{{strike}}'}, {'name': 'Expiration', 'value': '{{expiration}}'}]"
    )
    
    footer_template = models.TextField(
        help_text="Template for the footer of the signal e.g. '{{ticker}} is trading at {{strike}} and expires on {{expiration}}' will be replaced with the ticker, strike, and expiration values"
    )
    
    show_title_default = models.BooleanField(
        default=True,
        help_text="Default value for showing embed title when using this signal type"
    )
    
    show_description_default = models.BooleanField(
        default=True,
        help_text="Default value for showing embed description when using this signal type"
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = [['name', 'user']]
    
    def __str__(self):
        return self.name

class Signal(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='signals')
    signal_type = models.ForeignKey(SignalType, on_delete=models.CASCADE, related_name='signals')
    data = models.JSONField(default=dict, help_text="Signal data stored as key-value pairs based on the signal type's variables")
    discord_channel = models.ForeignKey('DiscordChannel', on_delete=models.SET_NULL, null=True, blank=True, related_name='signals', help_text="Discord channel to send this signal to")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.data.get('ticker', 'Unknown')} - {self.signal_type.name}"

class UserProfile(models.Model):
    """Extended user profile to store Discord information"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    discord_channel_name = models.CharField(max_length=255, blank=True, default='', help_text="Discord channel name (deprecated - use DiscordChannel model)")
    discord_channel_webhook = models.CharField(max_length=500, blank=True, default='', help_text="Discord channel webhook URL (deprecated - use DiscordChannel model)")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.user.username} - {self.discord_channel_name}"


class Agreement(models.Model):
    """
    Versioned agreement that users must accept.
    Only one Agreement should be active at a time (is_active=True).
    """
    version = models.CharField(max_length=40, unique=True)
    title = models.CharField(max_length=120, default="Crowned Trader Agreement")
    body = models.TextField(default="")
    is_active = models.BooleanField(default=True)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-is_active", "-published_at", "-id"]

    def save(self, *args, **kwargs):
        if self.is_active:
            Agreement.objects.filter(is_active=True).exclude(pk=self.pk).update(is_active=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.title} v{self.version}"


class AgreementAcceptance(models.Model):
    """
    Stores who/when/version agreed.
    """
    agreement = models.ForeignKey(Agreement, on_delete=models.CASCADE, related_name="acceptances")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="agreement_acceptances")
    accepted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [["agreement", "user"]]
        ordering = ["-accepted_at", "-id"]

    def __str__(self):
        return f"{self.user.username} accepted {self.agreement.version}"

class DiscordChannel(models.Model):
    """Model to store multiple Discord channels per user"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='discord_channels')
    channel_name = models.CharField(max_length=255, help_text="Discord channel name")
    webhook_url = models.CharField(max_length=500, help_text="Discord channel webhook URL")
    is_default = models.BooleanField(default=False, help_text="Default channel for sending signals")
    is_active = models.BooleanField(default=True, help_text="Whether this channel is active")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-is_default', 'channel_name']
        unique_together = [['user', 'channel_name']]
    
    def __str__(self):
        return f"{self.user.username} - {self.channel_name}"
    
    def save(self, *args, **kwargs):
        # Ensure only one default channel per user
        if self.is_default:
            DiscordChannel.objects.filter(user=self.user, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class UserTradePlan(models.Model):
    """
    Per-user saved Trade Plan defaults used on the Dashboard "Trade Plan" builder.

    Stored as JSON so the frontend can evolve without migrations for every minor tweak.
    """
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="trade_plan")
    plan = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - Trade Plan"


class UserTradePlanPreset(models.Model):
    """
    Named Trade Plan presets per user (selectable via dropdown on the Dashboard).
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="trade_plan_presets")
    name = models.CharField(max_length=80)
    plan = models.JSONField(default=dict, blank=True)
    is_default = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [["user", "name"]]
        ordering = ["-is_default", "-updated_at", "name"]

    def save(self, *args, **kwargs):
        # Ensure only one default preset per user.
        if self.is_default and self.user_id:
            UserTradePlanPreset.objects.filter(user_id=self.user_id, is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - {self.name}"


class Position(models.Model):
    """
    A user's open/closed position created from posted trades.
    Designed to support an exchange-style P/L view.
    """

    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_CLOSED, "Closed"),
    ]

    INSTRUMENT_OPTIONS = "options"
    INSTRUMENT_SHARES = "shares"
    INSTRUMENT_CHOICES = [
        (INSTRUMENT_OPTIONS, "Options"),
        (INSTRUMENT_SHARES, "Shares"),
    ]

    MODE_AUTO = "auto"
    MODE_MANUAL = "manual"
    MODE_CHOICES = [
        (MODE_AUTO, "Automatic"),
        (MODE_MANUAL, "Manual"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="positions")
    signal = models.ForeignKey("Signal", on_delete=models.SET_NULL, null=True, blank=True, related_name="positions")

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_OPEN)
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default=MODE_MANUAL)

    # Manual tracking state (used when mode == manual)
    tp_hit_level = models.IntegerField(default=0)  # 0 == none, 1 == TP1 hit, etc.
    sl_hit = models.BooleanField(default=False)
    closed_units = models.IntegerField(default=0)  # shares or (contracts * 100)
    realized_pnl = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    # Underlying symbol
    symbol = models.CharField(max_length=20, blank=True, default="")
    instrument = models.CharField(max_length=12, choices=INSTRUMENT_CHOICES, default=INSTRUMENT_OPTIONS)

    # Options metadata (optional; used when instrument == options)
    option_contract = models.CharField(max_length=64, blank=True, default="")
    option_type = models.CharField(max_length=10, blank=True, default="")  # CALL/PUT
    strike = models.CharField(max_length=32, blank=True, default="")
    expiration = models.CharField(max_length=32, blank=True, default="")  # YYYY-MM-DD

    quantity = models.IntegerField(default=1)
    multiplier = models.IntegerField(default=100)  # options are typically *100, shares *1

    entry_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    exit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    highest_price = models.DecimalField(
        max_digits=12,
        decimal_places=4,
        null=True,
        blank=True,
        help_text="Peak price seen for trailing-stop logic (auto tracking).",
    )

    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-opened_at", "-created_at"]

    def __str__(self):
        base = self.symbol or "Unknown"
        return f"{self.user.username} - {base} ({self.status})"

