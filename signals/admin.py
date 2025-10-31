from django.contrib import admin
from .models import Signal, SignalType

@admin.register(Signal)
class SignalAdmin(admin.ModelAdmin):
    list_display = ['get_ticker', 'signal_type', 'created_at']
    list_filter = ['signal_type', 'created_at']
    search_fields = ['data',]
    
    def get_ticker(self, obj):
        return obj.data.get('ticker', 'N/A')
    get_ticker.short_description = 'Ticker'

@admin.register(SignalType)
class SignalTypeAdmin(admin.ModelAdmin):
    list_display = ['name',]

