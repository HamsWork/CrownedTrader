from django import forms
from django.core.exceptions import ValidationError
from .models import Signal, SignalType
import json

class SignalForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Set default signal_type to first available signal type
        if 'signal_type' in self.fields:
            first_signal_type = SignalType.objects.first()
            if first_signal_type:
                self.fields['signal_type'].initial = first_signal_type.id
            # Remove the empty label (--------)
            self.fields['signal_type'].empty_label = None
    
    class Meta:
        model = Signal
        fields = ['signal_type', 'data']
        widgets = {
            'signal_type': forms.Select(attrs={
                'class': 'form-select'
            }),
            'data': forms.HiddenInput(attrs={
                'value': '{}'
            }),  # Hidden field, populated by JavaScript
        }
    
    def clean(self):
        cleaned_data = super().clean()
        signal_type = cleaned_data.get('signal_type')
        data = cleaned_data.get('data')
        
        # If data is None or empty, initialize as empty dict
        if data is None:
            data = {}
        
        # If data is a string (from hidden input), parse it as JSON
        if isinstance(data, str):
            try:
                data = json.loads(data) if data and data.strip() else {}
            except json.JSONDecodeError:
                data = {}
        
        # Ensure data is a dict
        if not isinstance(data, dict):
            data = {}
        
        # Validate required fields if signal_type is provided
        if signal_type:
            variables = signal_type.variables or []
            required_fields = []
            missing_fields = []
            
            # Find all required fields
            for var in variables:
                if isinstance(var, dict) and var.get('required', False):
                    field_name = var.get('name')
                    if field_name:  # Only add if name exists
                        required_fields.append(field_name)
            
            # Check if all required fields are present and not empty
            for field_name in required_fields:
                field_value = data.get(field_name)
                # Check if field is missing or empty
                is_empty = (
                    field_value is None or 
                    (isinstance(field_value, str) and field_value.strip() == '') or
                    (isinstance(field_value, (list, dict)) and len(field_value) == 0)
                )
                
                if is_empty:
                    # Get the label for better error message
                    var_info = next((v for v in variables if isinstance(v, dict) and v.get('name') == field_name), {})
                    label = var_info.get('label', field_name)
                    missing_fields.append(label)
            
            # Raise validation error if any required fields are missing
            if missing_fields:
                error_msg = f"The following required fields are missing: {', '.join(missing_fields)}"
                raise ValidationError({
                    'data': error_msg
                })
        
        # Update cleaned_data with parsed JSON (always update to ensure it's a dict)
        cleaned_data['data'] = data
        
        return cleaned_data
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        
        # Collect all dynamic field values into the data JSON field
        cleaned_data = self.cleaned_data
        
        # Get all variables from the signal type
        signal_type = cleaned_data.get('signal_type')
        if signal_type:
            instance.data = {}
            
            # Get data from POST data (we'll handle this in the view)
            if 'data' in cleaned_data and isinstance(cleaned_data['data'], dict):
                instance.data = cleaned_data['data']
        
        if commit:
            instance.save()
        return instance

