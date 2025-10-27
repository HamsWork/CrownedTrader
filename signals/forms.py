from django import forms
from .models import Signal, SignalType

class SignalForm(forms.ModelForm):
    class Meta:
        model = Signal
        fields = ['ticker', 'contract_info', 'signal_type', 'extra_info']
        widgets = {
            'ticker': forms.TextInput(attrs={
                'class': 'form-input',
                'placeholder': 'Enter ticker (e.g., AAPL, TSLA)'
            }),
            'contract_info': forms.Textarea(attrs={
                'class': 'form-textarea',
                'placeholder': 'Enter contract information',
                'rows': 4
            }),
            'signal_type': forms.Select(attrs={
                'class': 'form-select'
            }),
            'extra_info': forms.Textarea(attrs={
                'class': 'form-textarea',
                'placeholder': 'Additional information (optional)',
                'rows': 3
            }),
        }

