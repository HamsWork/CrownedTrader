from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from .models import Signal, SignalType
import json
import re

class SignalForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        # Set default signal_type (prefer "Common Trade Alert" if it exists)
        if 'signal_type' in self.fields:
            if self.user:
                signal_types = SignalType.objects.filter(
                    Q(user__isnull=True) | Q(user=self.user)
                )
            else:
                signal_types = SignalType.objects.filter(user__isnull=True)

            # Filter queryset to only show available signal types
            self.fields['signal_type'].queryset = signal_types

            preferred = signal_types.filter(name__iexact='Common Trade Alert').first()
            first_signal_type = preferred or signal_types.first()
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
            
            # Check if is_shares is checked
            is_shares = data.get('is_shares', 'false').lower() in ('true', '1', 'yes')
            
            # Find all required fields
            for var in variables:
                if isinstance(var, dict) and var.get('required', False):
                    field_name = var.get('name')
                    if field_name:  # Only add if name exists
                        # Skip option-related fields if is_shares is true
                        if is_shares and field_name in ['strike', 'expiration', 'option_type']:
                            continue
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


class VariableForm(forms.Form):
    """Form for a single variable in signal type"""
    name = forms.CharField(max_length=100, required=True)
    type = forms.ChoiceField(
        choices=[
            ('string', 'String'),
            ('float', 'Float'),
            ('integer', 'Integer'),
            ('date', 'Date'),
            ('boolean', 'Boolean'),
            ('text', 'Text'),
            ('select', 'Select'),
            ('ticker_select', 'Ticker Select'),
            ('ticker_type', 'Ticker Type'),
        ],
        required=True
    )
    label = forms.CharField(max_length=200, required=True)
    required = forms.BooleanField(required=False, initial=False)
    hint = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={'rows': 2}))
    options = forms.CharField(max_length=500, required=False, help_text="For select type, comma-separated options (e.g., PUT, CALL)")
    default = forms.CharField(max_length=200, required=False)


class FieldTemplateForm(forms.Form):
    """Form for a single field in fields_template"""
    name = forms.CharField(max_length=200, required=True, help_text="Field name (can use {{variable}} syntax)")
    value = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={'rows': 3}), help_text="Field value (can use {{variable}} syntax)")
    inline = forms.BooleanField(required=False, initial=False)


class SignalTypeForm(forms.ModelForm):
    """Form for creating/editing signal types"""
    variables_json = forms.CharField(
        widget=forms.HiddenInput(),
        required=False,
        help_text="JSON array of variables"
    )
    fields_template_json = forms.CharField(
        widget=forms.HiddenInput(),
        required=False,
        help_text="JSON array of field templates"
    )
    
    class Meta:
        model = SignalType
        fields = ['name', 'title_template', 'description_template', 'color', 'footer_template', 'show_title_default', 'show_description_default']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input'}),
            'title_template': forms.TextInput(attrs={'class': 'form-input'}),
            'description_template': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 3}),
            'color': forms.TextInput(attrs={'class': 'form-input'}),
            'footer_template': forms.Textarea(attrs={'class': 'form-textarea', 'rows': 2}),
            'show_title_default': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
            'show_description_default': forms.CheckboxInput(attrs={'class': 'form-checkbox'}),
        }
    
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # These templates are optional
        self.fields['description_template'].required = False
        self.fields['footer_template'].required = False
        
        if self.instance and self.instance.pk:
            # Editing existing signal type
            if self.instance.variables:
                self.fields['variables_json'].initial = json.dumps(self.instance.variables, indent=2)
            if self.instance.fileds_template:
                self.fields['fields_template_json'].initial = json.dumps(self.instance.fileds_template, indent=2)
    
    def clean_name(self):
        name = self.cleaned_data.get('name')
        if not name:
            raise ValidationError('Name is required.')
        
        # Check uniqueness for this user
        existing = SignalType.objects.filter(name=name, user=self.user)
        if self.instance and self.instance.pk:
            existing = existing.exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError(f'A signal type with name "{name}" already exists.')
        
        return name
    
    def clean_variables_json(self):
        variables_json = self.cleaned_data.get('variables_json', '[]')
        try:
            variables = json.loads(variables_json) if variables_json else []
            if not isinstance(variables, list):
                raise ValidationError('Variables must be a JSON array.')
            
            # Validate each variable
            variable_names = set()
            for var in variables:
                if not isinstance(var, dict):
                    raise ValidationError('Each variable must be an object.')
                
                var_name = var.get('name', '').strip()
                if not var_name:
                    raise ValidationError('Each variable must have a name.')
                
                if var_name in variable_names:
                    raise ValidationError(f'Duplicate variable name: {var_name}')
                variable_names.add(var_name)
                
                # Validate variable name format (alphanumeric and underscore only)
                if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', var_name):
                    raise ValidationError(f'Invalid variable name "{var_name}". Use only letters, numbers, and underscores, starting with a letter or underscore.')
                
                var_type = var.get('type')
                if var_type not in ['string', 'float', 'integer', 'date', 'boolean', 'text', 'select', 'ticker_select', 'ticker_type']:
                    raise ValidationError(f'Invalid variable type: {var_type}')
                
                # ticker_select can use global US ticker list; custom options are optional.
                if var_type == 'select' and not var.get('options'):
                    raise ValidationError(f'Select type variable "{var_name}" must have options.')
            
            return variables
        except json.JSONDecodeError:
            raise ValidationError('Invalid JSON format for variables.')
    
    def clean_fields_template_json(self):
        fields_template_json = self.cleaned_data.get('fields_template_json', '[]')
        try:
            fields_template = json.loads(fields_template_json) if fields_template_json else []
            if not isinstance(fields_template, list):
                raise ValidationError('Fields template must be a JSON array.')
            
            # Validate each field
            for field in fields_template:
                if not isinstance(field, dict):
                    raise ValidationError('Each field must be an object.')
                
                if 'name' not in field and 'value' not in field:
                    raise ValidationError('Each field must have at least a name or value.')
            
            return fields_template
        except json.JSONDecodeError:
            raise ValidationError('Invalid JSON format for fields template.')

    def save(self, commit=True):
        """
        Persist the JSON editor fields into the model's JSONFields.

        The UI edits `variables_json` and `fields_template_json`; these are not model
        fields, so we must copy them into `SignalType.variables` and
        `SignalType.fileds_template`.
        """
        instance = super().save(commit=False)
        instance.variables = self.cleaned_data.get('variables_json') or []
        instance.fileds_template = self.cleaned_data.get('fields_template_json') or []
        if commit:
            instance.save()
        return instance
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        
        # Set user
        if self.user:
            instance.user = self.user
        
        # Set variables and fields_template from cleaned data (already parsed by clean methods)
        # The clean methods return the parsed list, not the JSON string
        variables = self.cleaned_data.get('variables_json', [])
        fields_template = self.cleaned_data.get('fields_template_json', [])
        
        # Ensure they are lists
        if isinstance(variables, list):
            instance.variables = variables
        else:
            # Fallback: try to parse if it's still a string
            try:
                instance.variables = json.loads(variables) if variables else []
            except (json.JSONDecodeError, TypeError):
                instance.variables = []
        
        if isinstance(fields_template, list):
            instance.fileds_template = fields_template
        else:
            # Fallback: try to parse if it's still a string
            try:
                instance.fileds_template = json.loads(fields_template) if fields_template else []
            except (json.JSONDecodeError, TypeError):
                instance.fileds_template = []
        
        if commit:
            instance.save()
        
        return instance

