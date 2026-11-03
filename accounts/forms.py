from django import forms
from django.contrib.auth.forms import UserCreationForm

from accounts.models import Address, User
from catalogue.regions import region_for_postal_code


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleImageField(forms.ImageField):
    """Accepts several images from one <input multiple>. Returns a list."""

    def __init__(self, *args, max_files=5, **kwargs):
        self.max_files = max_files
        kwargs.setdefault('widget', MultipleFileInput(attrs={'accept': 'image/*'}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        files = data if isinstance(data, (list, tuple)) else ([data] if data else [])
        if len(files) > self.max_files:
            raise forms.ValidationError(f'Upload at most {self.max_files} photos.')
        if self.required and not files:
            raise forms.ValidationError('Please upload at least one photo.')
        return [super(MultipleImageField, self).clean(f, initial) for f in files]


class RegisterForm(UserCreationForm):
    """Homeowner registration (H01). The email is also stored as the username."""

    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150)
    email = forms.EmailField()
    mobile = forms.RegexField(regex=r'^[689]\d{7}$', max_length=8,
                              error_messages={'invalid': 'Enter an 8-digit Singapore mobile number.'})

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'mobile']

    def clean_email(self):
        email = self.cleaned_data['email'].lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def save(self, commit=True, role=User.Role.HOMEOWNER):
        user = super().save(commit=False)
        user.username = user.email
        user.role = role
        if commit:
            user.save()
        return user


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'mobile']


class AddressForm(forms.ModelForm):
    class Meta:
        model = Address
        fields = ['label', 'street', 'unit', 'postal_code']
        labels = {'street': 'Block and street', 'unit': 'Unit number'}

    def clean_postal_code(self):
        postal_code = self.cleaned_data['postal_code'].strip()
        region = region_for_postal_code(postal_code)
        if not region:
            raise forms.ValidationError('Enter a valid 6-digit Singapore postal code.')
        # The planning region is a pricing model input, derived here from the lookup table.
        self.instance.planning_region = region
        return postal_code


class ReasonForm(forms.Form):
    """Used wherever an action needs a reason: rejections, suspensions, cancellations."""
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}))
