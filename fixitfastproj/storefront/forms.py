from django import forms
from django.conf import settings

from accounts.forms import MultipleImageField
from catalogue.models import JobType


class GroupedJobTypeField(forms.ModelChoiceField):
    """A job type <select> grouped by category, so no JavaScript is needed for two dropdowns."""

    def build_grouped_choices(self):
        groups = {}

        for job_type in self.queryset.select_related('category'):
            groups.setdefault(job_type.category.name, []).append(
                (job_type.pk, job_type.name)
            )
        self.choices = [('', 'Select a repair')] + list(groups.items())


class JobRequestForm(forms.Form):
    """H03: describe the problem once, with photos and an address."""

    job_type = GroupedJobTypeField(
        queryset=JobType.objects.filter(
            is_active=True,
            category__is_active=True,
        ),
        label='What needs fixing?',
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={'rows': 4, 'placeholder': 'e.g. The kitchen tap drips constantly and the handle is loose.'}),
        min_length=settings.MIN_DESCRIPTION_LENGTH,
        help_text=f'At least {settings.MIN_DESCRIPTION_LENGTH} characters.')
    photos = MultipleImageField(required=False, max_files=settings.MAX_REQUEST_PHOTOS,
                                help_text=f'Up to {settings.MAX_REQUEST_PHOTOS} photos help the handyman quote.')
    address = forms.ModelChoiceField(queryset=None, empty_label=None)

    def __init__(self, *args, homeowner, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['job_type'].build_grouped_choices()
        self.fields['address'].queryset = homeowner.addresses.order_by(
            '-is_default',
            'label',
        )


class RatingForm(forms.Form):
    rating = forms.TypedChoiceField(choices=[(i, f'{i} star{"s" if i > 1 else ""}') for i in range(5, 0, -1)],
                                    coerce=int, widget=forms.RadioSelect)
    comment = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 3}))


class ProblemForm(forms.Form):
    note = forms.CharField(label='What is wrong?', widget=forms.Textarea(attrs={'rows': 3}))


class ClaimForm(forms.Form):
    description = forms.CharField(label='Describe the problem', min_length=settings.MIN_DESCRIPTION_LENGTH,
                                  widget=forms.Textarea(attrs={'rows': 4}))
    photos = MultipleImageField(required=False, max_files=settings.MAX_REQUEST_PHOTOS)
