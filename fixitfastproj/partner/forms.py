from datetime import datetime

from django import forms
from django.conf import settings

from accounts.forms import MultipleImageField, RegisterForm
from catalogue.models import JobType, Tier
from handymen.models import AvailabilityException, Document, HandymanProfile
from handymen.services import SLOT_START_TIMES
from jobs.models import JobPhoto
from pricing.predictor import is_within_range

DATE_INPUT = forms.DateInput(attrs={'type': 'date'})


class ApplicationForm(RegisterForm):
    """P01: account details, experience, job types and documents in one form."""

    years_experience = forms.IntegerField(min_value=0, max_value=60)
    job_types = forms.ModelMultipleChoiceField(
        queryset=JobType.objects.filter(is_active=True, min_tier=Tier.STANDARD, licence_required=False),
        widget=forms.CheckboxSelectMultiple,
        help_text='New partners start at Standard. More job types open up as you move up a tier.')
    identity_document = forms.FileField(help_text='NRIC or work pass, front and back.')
    certificate = forms.FileField(required=False)
    certificate_title = forms.CharField(required=False, max_length=150)
    certificate_expiry = forms.DateField(required=False, widget=DATE_INPUT)
    licence = forms.FileField(required=False, help_text='Required for electrical and gas work.')
    licence_title = forms.CharField(required=False, max_length=150)
    licence_expiry = forms.DateField(required=False, widget=DATE_INPUT)
    insurance = forms.FileField(required=False, label='Public liability insurance')
    insurance_expiry = forms.DateField(required=False, widget=DATE_INPUT)

    def clean(self):
        data = super().clean()
        for name in ('certificate', 'licence'):
            if data.get(name) and not data.get(f'{name}_title'):
                self.add_error(f'{name}_title', 'Please name the document, e.g. "PUB Licensed Plumber".')
        return data


class HandymanProfileForm(forms.ModelForm):
    mobile = forms.CharField(max_length=20)

    class Meta:
        model = HandymanProfile
        fields = ['photo', 'bio', 'years_experience', 'job_types']
        widgets = {'job_types': forms.CheckboxSelectMultiple, 'bio': forms.Textarea(attrs={'rows': 3})}

    def __init__(self, *args, allowed_job_types, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['mobile'].initial = self.instance.user.mobile
        # Only job types at or below the current tier, and licensed ones once verified (P02).
        self.fields['job_types'].queryset = allowed_job_types

    def save(self, commit=True):
        profile = super().save(commit)
        profile.user.mobile = self.cleaned_data['mobile']
        profile.user.save()
        return profile


class DocumentForm(forms.ModelForm):
    class Meta:
        model = Document
        fields = ['doc_type', 'title', 'file', 'job_types', 'expiry_date']
        widgets = {'job_types': forms.CheckboxSelectMultiple, 'expiry_date': DATE_INPUT}
        help_texts = {'job_types': 'For certificates and licences: the job types it covers.'}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['job_types'].queryset = JobType.objects.filter(is_active=True)
        self.fields['job_types'].required = False


class ExceptionForm(forms.ModelForm):
    start_time = forms.TypedChoiceField(
        choices=[('', 'Whole day')] + [(t.strftime('%H:%M'), t.strftime('%I:%M %p')) for t in SLOT_START_TIMES],
        required=False, empty_value=None, label='Slot',
        coerce=lambda value: datetime.strptime(value, '%H:%M').time())

    class Meta:
        model = AvailabilityException
        fields = ['date', 'start_time', 'reason']
        widgets = {'date': DATE_INPUT}


class QuotationForm(forms.Form):
    """P05: a justification is required when the amount is outside the predicted range."""

    amount = forms.DecimalField(min_value=1, max_digits=8, decimal_places=2, label='Quotation (S$)')
    work_description = forms.CharField(widget=forms.Textarea(attrs={'rows': 3}), label='Work to be done')
    justification = forms.CharField(
        required=False, widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Required if your amount is outside the predicted range.')

    def __init__(self, *args, job, **kwargs):
        self.job = job
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        amount = data.get('amount')
        if amount is not None and not is_within_range(self.job, amount) and not data.get('justification'):
            self.add_error('justification', 'Your amount is outside the predicted range. Please explain why.')
        return data


class JobPhotoForm(forms.Form):
    kind = forms.ChoiceField(choices=[(JobPhoto.Kind.BEFORE, 'Before work'), (JobPhoto.Kind.AFTER, 'After work')])
    images = MultipleImageField(max_files=settings.MAX_REQUEST_PHOTOS, label='Photos')


class PartForm(forms.Form):
    name = forms.CharField(max_length=150, label='Part name')
    price = forms.DecimalField(min_value=0.5, max_digits=8, decimal_places=2, label='Price (S$)')
    receipt = forms.ImageField(label='Receipt photo')


class QuizForm(forms.Form):
    """One radio group per question, built from the fixture (P03)."""

    def __init__(self, *args, questions, **kwargs):
        super().__init__(*args, **kwargs)
        self.questions = list(questions)
        for q in self.questions:
            self.fields[f'q{q.pk}'] = forms.ChoiceField(
                label=q.text, widget=forms.RadioSelect,
                choices=[('a', q.option_a), ('b', q.option_b), ('c', q.option_c), ('d', q.option_d)])

    def score(self):
        correct = sum(self.cleaned_data[f'q{q.pk}'] == q.correct_option for q in self.questions)
        return correct, len(self.questions)
