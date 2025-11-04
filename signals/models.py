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
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.data.get('ticker', 'Unknown')} - {self.signal_type.name}"

class UserProfile(models.Model):
    """Extended user profile to store Discord information"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    discord_channel_name = models.CharField(max_length=255, help_text="Discord channel name")
    discord_channel_webhook = models.URLField(max_length=500, help_text="Discord channel webhook URL")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"{self.user.username} - {self.discord_channel_name}"

