from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from accounts.models import Address
from catalogue.models import JobType, PlanningRegion, Tier, TimingBand
from handymen.models import HandymanProfile

User = settings.AUTH_USER_MODEL


class Job(models.Model):
    """One repair request, from draft (H03) to completion and warranty (H11)."""

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        REQUESTED = 'REQUESTED', 'Requested'
        HANDYMAN_ASSIGNED = 'ASSIGNED', 'Handyman Assigned'
        QUOTATION_PENDING = 'QUOTE_PENDING', 'Quotation Pending'
        QUOTATION_UNDER_REVIEW = 'QUOTE_REVIEW', 'Quotation Under Review'
        AWAITING_PAYMENT = 'AWAITING_PAYMENT', 'Awaiting Payment'
        SCHEDULED = 'SCHEDULED', 'Scheduled'
        ON_THE_WAY = 'ON_THE_WAY', 'On the Way'
        IN_PROGRESS = 'IN_PROGRESS', 'In Progress'
        COMPLETED = 'COMPLETED', 'Completed'
        CANCELLED = 'CANCELLED', 'Cancelled'
        DECLINED = 'DECLINED', 'Declined'

    homeowner = models.ForeignKey(User, on_delete=models.PROTECT, related_name='jobs')
    job_type = models.ForeignKey(JobType, on_delete=models.PROTECT, related_name='jobs')
    address = models.ForeignKey(Address, on_delete=models.PROTECT, related_name='jobs')
    description = models.TextField()
    tier = models.CharField(max_length=10, choices=Tier.choices, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)

    # Chosen slot (H05). slot_end is slot_start + settings.SLOT_HOURS.
    slot_start = models.DateTimeField(null=True, blank=True)
    slot_end = models.DateTimeField(null=True, blank=True)
    timing_band = models.CharField(max_length=10, choices=TimingBand.choices, blank=True)
    # Copied from the address when the slot is chosen, so the model input is kept with the job.
    planning_region = models.CharField(max_length=10, choices=PlanningRegion.choices, blank=True)

    # Saved pricing model output (H05). Every later decision uses these values,
    # so the range the homeowner accepted cannot change during the job.
    price_p10 = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    price_p50 = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    price_p90 = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    model_version = models.CharField(max_length=30, blank=True)

    handyman = models.ForeignKey(
        HandymanProfile, on_delete=models.PROTECT, null=True, blank=True, related_name='jobs')
    quote_deadline = models.DateTimeField(null=True, blank=True)
    service_fee_waived = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    requested_at = models.DateTimeField(null=True, blank=True)
    matched_at = models.DateTimeField(null=True, blank=True)    # launch measure: matched within 24h
    completed_at = models.DateTimeField(null=True, blank=True)  # handyman marked Completed
    confirmed_at = models.DateTimeField(null=True, blank=True)  # homeowner confirmed; warranty starts
    warranty_ends_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(blank=True)
    # Set when the homeowner reports a problem instead of confirming (H10); pauses auto-confirm.
    problem_note = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'Job #{self.pk} - {self.job_type}'

    # Statuses in which the job holds the handyman's slot.
    ACTIVE_STATUSES = [
        Status.HANDYMAN_ASSIGNED, Status.QUOTATION_PENDING, Status.QUOTATION_UNDER_REVIEW,
        Status.AWAITING_PAYMENT, Status.SCHEDULED, Status.ON_THE_WAY, Status.IN_PROGRESS,
    ]
    CLOSED_STATUSES = [Status.COMPLETED, Status.CANCELLED, Status.DECLINED]

    @property
    def is_paid(self):
        return self.ledger.filter(entry_type=LedgerEntry.EntryType.QUOTATION_HELD).exists()

    @property
    def can_cancel(self):
        # Homeowner can cancel until the handyman marks the job In Progress (H08).
        return self.status not in self.CLOSED_STATUSES + [
            self.Status.IN_PROGRESS, self.Status.DRAFT]

    @property
    def accepted_quotation(self):
        return self.quotations.filter(status=Quotation.Status.ACCEPTED).first()

    @property
    def released_quotation(self):
        return self.quotations.filter(status=Quotation.Status.RELEASED).first()

    @property
    def warranty_active(self):
        from django.utils import timezone
        return bool(self.warranty_ends_at and timezone.now() <= self.warranty_ends_at)


class JobPhoto(models.Model):
    class Kind(models.TextChoices):
        REQUEST = 'REQUEST', 'Request photo'
        BEFORE = 'BEFORE', 'Before work'
        AFTER = 'AFTER', 'After work'

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='photos')
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.REQUEST)
    image = models.ImageField(upload_to='jobs/photos/')
    uploaded_at = models.DateTimeField(auto_now_add=True)


class JobStatusEvent(models.Model):
    """Timeline entry shown on the job tracking page (H08)."""

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='events')
    status = models.CharField(max_length=16, choices=Job.Status.choices)
    note = models.CharField(max_length=255, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


class Recommendation(models.Model):
    """A handyman proposed for a job by the rule-based scoring (H06, P04).

    Rows are created in score order. Declines by either side move the job to the
    next PROPOSED row; when none is left the homeowner picks another slot or tier.
    """

    class Status(models.TextChoices):
        PROPOSED = 'PROPOSED', 'Proposed'
        ACCEPTED = 'ACCEPTED', 'Accepted by homeowner'
        HOMEOWNER_DECLINED = 'HO_DECLINED', 'Declined by homeowner'
        HANDYMAN_DECLINED = 'HM_DECLINED', 'Declined by handyman'
        EXPIRED = 'EXPIRED', 'Quote deadline passed'
        QUOTE_REJECTED = 'QUOTE_REJECTED', 'Quotation rejected'
        UNAVAILABLE = 'UNAVAILABLE', 'No longer free for the slot'

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='recommendations')
    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='recommendations')
    score = models.DecimalField(max_digits=6, decimal_places=3)
    rank = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROPOSED)
    decline_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['job', 'rank']


class Quotation(models.Model):
    """A handyman's fixed quotation (P05), released automatically or by an employee (A06)."""

    class Status(models.TextChoices):
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under review'
        RETURNED = 'RETURNED', 'Returned to handyman'
        RELEASED = 'RELEASED', 'Released to homeowner'
        ACCEPTED = 'ACCEPTED', 'Accepted and paid'
        REJECTED = 'REJECTED', 'Rejected by homeowner'

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='quotations')
    handyman = models.ForeignKey(HandymanProfile, on_delete=models.PROTECT, related_name='quotations')
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    work_description = models.TextField()
    justification = models.TextField(blank=True, help_text='Required when outside the predicted range')
    status = models.CharField(max_length=12, choices=Status.choices)
    # True when the amount was inside the predicted range and released without staff review.
    auto_released = models.BooleanField(default=False)

    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    @property
    def in_range(self):
        return self.job.price_p10 <= self.amount <= self.job.price_p90


class PartLine(models.Model):
    """A replacement part added during the visit (P07, H09, A07)."""

    class HomeownerDecision(models.TextChoices):
        PENDING = 'PENDING', 'Awaiting homeowner'
        APPROVED = 'APPROVED', 'Approved'
        DECLINED = 'DECLINED', 'Declined'

    class ReviewStatus(models.TextChoices):
        NOT_REQUIRED = 'NOT_REQUIRED', 'Not required'
        PENDING = 'PENDING', 'Pending review'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='parts')
    name = models.CharField(max_length=150)
    price = models.DecimalField(max_digits=8, decimal_places=2)
    receipt = models.ImageField(upload_to='jobs/receipts/')
    homeowner_decision = models.CharField(
        max_length=8, choices=HomeownerDecision.choices, default=HomeownerDecision.PENDING)
    # Flagged when the revised total exceeds the saved 90th percentile; payout held until reviewed.
    flagged = models.BooleanField(default=False)
    review_status = models.CharField(
        max_length=12, choices=ReviewStatus.choices, default=ReviewStatus.NOT_REQUIRED)
    review_reason = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class LedgerEntry(models.Model):
    """Simulated payment ledger for a job (A10, Table 2). Amounts are always positive;
    entry_type says which way the money moved."""

    class EntryType(models.TextChoices):
        QUOTATION_HELD = 'QUOTE_HELD', 'Quotation payment held'
        SERVICE_FEE = 'SERVICE_FEE', 'Service fee'
        SERVICE_FEE_WAIVED = 'FEE_WAIVED', 'Service fee waived (first job)'
        PART_PAYMENT = 'PART_PAYMENT', 'Part payment'
        CONSULTATION_FEE = 'CONSULT_FEE', 'Consultation fee'
        REFUND = 'REFUND', 'Refund to homeowner'
        COMMISSION = 'COMMISSION', 'Platform commission'
        PAYOUT = 'PAYOUT', 'Handyman payout'
        PAYOUT_HELD = 'PAYOUT_HELD', 'Payout held for review'

    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='ledger')
    entry_type = models.CharField(max_length=12, choices=EntryType.choices)
    amount = models.DecimalField(max_digits=8, decimal_places=2)
    commission_rate = models.DecimalField(max_digits=4, decimal_places=3, null=True, blank=True)
    part = models.ForeignKey(PartLine, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    note = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name_plural = 'ledger entries'


class Review(models.Model):
    """Homeowner's rating, once per job (H10). Feeds tier eligibility (A05)."""

    job = models.OneToOneField(Job, on_delete=models.CASCADE, related_name='review')
    handyman = models.ForeignKey(HandymanProfile, on_delete=models.CASCADE, related_name='reviews')
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class WarrantyClaim(models.Model):
    """Raised by the homeowner (H11) and handled by an employee by tier (A08)."""

    class Status(models.TextChoices):
        SUBMITTED = 'SUBMITTED', 'Submitted'
        UNDER_REVIEW = 'UNDER_REVIEW', 'Under Review'
        RETURN_VISIT_SCHEDULED = 'RETURN_VISIT', 'Return Visit Scheduled'
        RESOLVED = 'RESOLVED', 'Resolved'
        REJECTED = 'REJECTED', 'Rejected'

    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name='claims')
    description = models.TextField()
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.SUBMITTED)

    refund_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    return_visit_at = models.DateTimeField(null=True, blank=True)
    # Platinum only: assigned when the original handyman is unavailable within three days.
    replacement_handyman = models.ForeignKey(
        HandymanProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name='replacement_claims')
    resolution_note = models.TextField(blank=True)
    handled_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']


class ClaimPhoto(models.Model):
    claim = models.ForeignKey(WarrantyClaim, on_delete=models.CASCADE, related_name='photos')
    image = models.ImageField(upload_to='claims/photos/')
