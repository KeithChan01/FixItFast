from django import forms
from django.utils import timezone

from catalogue.models import Category, JobType, Tier, TimingBand
from handymen.models import HandymanProfile
from jobs.models import Job
from pricing.predictor import known_job_types


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'description', 'is_active']
        widgets = {'description': forms.Textarea(attrs={'rows': 2})}


class JobTypeForm(forms.ModelForm):
    class Meta:
        model = JobType
        fields = ['category', 'name', 'description', 'min_tier', 'licence_required', 'is_active']
        widgets = {'description': forms.Textarea(attrs={'rows': 3})}
        labels = {'min_tier': 'Minimum tier'}

    def clean(self):
        data = super().clean()
        # The pricing model can only predict for job types it was trained on (A03).
        if data.get('is_active') and data.get('name') and data['name'] not in known_job_types():
            self.add_error('is_active', 'This job type is not in the pricing model yet. '
                                        'It must stay inactive until the model is retrained to include it.')
        return data


class DowngradeForm(forms.Form):
    to_tier = forms.ChoiceField(choices=Tier.choices, label='Move to tier')
    reason = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, current_tier, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['to_tier'].choices = [(t, Tier(t).label) for t in Tier.values[:Tier.rank(current_tier)]]


class RefundForm(forms.Form):
    amount = forms.DecimalField(min_value=0.01, max_digits=8, decimal_places=2, label='Refund amount (S$)')
    note = forms.CharField(widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, max_amount, **kwargs):
        super().__init__(*args, **kwargs)
        self.max_amount = max_amount
        self.fields['amount'].help_text = f'Up to S${max_amount} paid on this job.'

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount > self.max_amount:
            raise forms.ValidationError(f'Cannot refund more than S${self.max_amount}.')
        return amount


class ReturnVisitForm(forms.Form):
    when = forms.DateTimeField(widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}), label='Return visit')
    replacement = forms.ModelChoiceField(queryset=HandymanProfile.objects.none(), required=False,
                                         label='Replacement Platinum handyman',
                                         help_text='Only if the original handyman is unavailable within three days.')
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}))

    def __init__(self, *args, job, **kwargs):
        super().__init__(*args, **kwargs)
        if job.tier == Tier.PLATINUM:
            self.fields['replacement'].queryset = HandymanProfile.objects.filter(
                tier=Tier.PLATINUM, application_status=HandymanProfile.ApplicationStatus.APPROVED,
                is_suspended=False, job_types=job.job_type).exclude(pk=job.handyman_id).select_related('user')
        else:
            del self.fields['replacement']

    def clean_when(self):
        when = self.cleaned_data['when']
        if when <= timezone.now():
            raise forms.ValidationError('Choose a time in the future.')
        return when


class JobFilterForm(forms.Form):
    """A10 filters: status, tier, category, timing band and date."""
    status = forms.ChoiceField(required=False, choices=[('', 'Any status')] + list(Job.Status.choices))
    tier = forms.ChoiceField(required=False, choices=[('', 'Any tier')] + list(Tier.choices))
    category = forms.ModelChoiceField(required=False, queryset=Category.objects.all(), empty_label='Any category')
    timing_band = forms.ChoiceField(required=False, choices=[('', 'Any band')] + list(TimingBand.choices))
    date = forms.DateField(required=False, widget=forms.DateInput(attrs={'type': 'date'}))
