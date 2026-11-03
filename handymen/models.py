from django.conf import settings
from django.db import models

from catalogue.models import JobType, Tier


class HandymanProfile(models.Model):
    """A handyman's application and partner record (P01, P02, A04, A05)."""

    class ApplicationStatus(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='handyman')
    photo = models.ImageField(upload_to='handymen/photos/', blank=True)
    bio = models.TextField(blank=True)
    years_experience = models.PositiveSmallIntegerField(default=0)
    job_types = models.ManyToManyField(JobType, related_name='handymen', blank=True)
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.STANDARD)

    application_status = models.CharField(
        max_length=10, choices=ApplicationStatus.choices, default=ApplicationStatus.PENDING)
    rejection_reason = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')

    # A suspended handyman receives no new jobs (A05).
    is_suspended = models.BooleanField(default=False)
    suspension_reason = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.user} ({self.get_tier_display()})'


class Document(models.Model):
    """Identity document, certificate, licence or insurance, verified by an employee (A04)."""

    class DocType(models.TextChoices):
        IDENTITY = 'IDENTITY', 'Identity document'
        CERTIFICATE = 'CERTIFICATE', 'Certificate'
        LICENCE = 'LICENCE', 'Licence'
        INSURANCE = 'INSURANCE', 'Public liability insurance'

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='documents')
    doc_type = models.CharField(max_length=12, choices=DocType.choices)
    title = models.CharField(max_length=150, help_text='e.g. Licensed Plumber certificate')
    file = models.FileField(upload_to='handymen/documents/')
    # Certificates and licences cover specific job types (Gold eligibility, licensed jobs).
    job_types = models.ManyToManyField(JobType, related_name='+', blank=True)
    expiry_date = models.DateField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.get_doc_type_display()}: {self.title}'


class TierChange(models.Model):
    """Tier history: upgrades confirmed and downgrades made by an employee (A05)."""

    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='tier_changes')
    from_tier = models.CharField(max_length=10, choices=Tier.choices)
    to_tier = models.CharField(max_length=10, choices=Tier.choices)
    reason = models.TextField()
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class WeeklyAvailability(models.Model):
    """One two-hour slot in a handyman's weekly template (P02)."""

    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='weekly_slots')
    weekday = models.PositiveSmallIntegerField(choices=[
        (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
        (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
    ])
    start_time = models.TimeField()

    class Meta:
        ordering = ['weekday', 'start_time']
        constraints = [
            models.UniqueConstraint(fields=['handyman', 'weekday', 'start_time'], name='unique_weekly_slot'),
        ]


class AvailabilityException(models.Model):
    """A date the handyman is away, e.g. leave. Blank start_time means the whole day."""

    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='exceptions')
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=150, blank=True)

    class Meta:
        ordering = ['date', 'start_time']


class TrainingModule(models.Model):
    """Platinum service-standards training content, loaded from a fixture (P03)."""

    order = models.PositiveSmallIntegerField(unique=True)
    title = models.CharField(max_length=150)
    content = models.TextField()

    class Meta:
        ordering = ['order']

    def __str__(self):
        return self.title


class QuizQuestion(models.Model):
    module = models.ForeignKey(TrainingModule, on_delete=models.CASCADE, related_name='questions')
    text = models.CharField(max_length=255)
    option_a = models.CharField(max_length=200)
    option_b = models.CharField(max_length=200)
    option_c = models.CharField(max_length=200)
    option_d = models.CharField(max_length=200)
    correct_option = models.CharField(max_length=1, choices=[(c, c.upper()) for c in 'abcd'])

    def __str__(self):
        return self.text


class QuizAttempt(models.Model):
    """Pass mark is in settings.QUIZ_PASS_MARK; a failed quiz can be retaken after 24 hours."""

    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='quiz_attempts')
    score = models.DecimalField(max_digits=4, decimal_places=3, help_text='Fraction correct, 0 to 1')
    passed = models.BooleanField()
    taken_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-taken_at']
