from django.contrib import admin
from .models import Signal, SignalType, UserProfile, DiscordChannel, UserTradePlan, UserTradePlanPreset

@admin.register(Signal)
class SignalAdmin(admin.ModelAdmin):
    list_display = ['get_ticker', 'user', 'signal_type', 'created_at']
    list_filter = ['signal_type', 'created_at', 'user']
    search_fields = ['data', 'user__username']
    
    def get_ticker(self, obj):
        return obj.data.get('ticker', 'N/A')
    get_ticker.short_description = 'Ticker'

@admin.register(SignalType)
class SignalTypeAdmin(admin.ModelAdmin):
    list_display = ['name',]

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'discord_channel_name', 'created_at']
    list_filter = ['created_at']
    search_fields = ['user__username', 'discord_channel_name']

@admin.register(DiscordChannel)
class DiscordChannelAdmin(admin.ModelAdmin):
    list_display = ['user', 'channel_name', 'is_default', 'is_active', 'created_at']
    list_filter = ['is_default', 'is_active', 'created_at']
    search_fields = ['user__username', 'channel_name', 'webhook_url']
    list_editable = ['is_default', 'is_active']


@admin.register(UserTradePlan)
class UserTradePlanAdmin(admin.ModelAdmin):
    list_display = ['user', 'updated_at']
    search_fields = ['user__username', 'user__email']


@admin.register(UserTradePlanPreset)
class UserTradePlanPresetAdmin(admin.ModelAdmin):
    list_display = ['user', 'name', 'is_default', 'updated_at']
    list_filter = ['is_default', 'updated_at']
    search_fields = ['user__username', 'user__email', 'name']
    list_editable = ['is_default']

