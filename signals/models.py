from django.db import models

class SignalType(models.TextChoices):
    ENTRY = 'entry', 'Entry'
    STOP_LOSS_HIT = 'stop_loss', 'Stop Loss Hit'
    TAKE_PROFIT = 'take_profit', 'Take Profit Hit'

class Signal(models.Model):
    ticker = models.CharField(max_length=50)
    contract_info = models.TextField()
    signal_type = models.CharField(max_length=20, choices=SignalType.choices)
    extra_info = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.ticker} - {self.get_signal_type_display()}"

