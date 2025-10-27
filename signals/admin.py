from django.contrib import admin
from .models import Signal

@admin.register(Signal)
class SignalAdmin(admin.ModelAdmin):
    list_display = ['ticker', 'signal_type', 'created_at']
    list_filter = ['signal_type', 'created_at']
    search_fields = ['ticker', 'contract_info']

