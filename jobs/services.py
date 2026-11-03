"""Job lifecycle: booking, matching, quotations, parts, payments and warranty.

Views call these functions so that every status change, ledger entry and
notification happens in one place. Payments are simulated (A10): "paying"
just records ledger entries.
"""
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from accounts.services import notify, notify_staff
from handymen.services import (
    commission_rate, eligible_handymen, free_slots, is_free, recommendation_score, timing_band_for,
)
from jobs.models import (
    ClaimPhoto, Job, JobStatusEvent, LedgerEntry, PartLine, Quotation, Recommendation, Review,
    WarrantyClaim,
)
from pricing.predictor import exceeds_upper_range, is_within_range

Status = Job.Status
Entry = LedgerEntry.EntryType


def money(value):
    return Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def set_status(job, status, actor=None, note=''):
    """Change the job status and add a timeline entry (H08)."""
    job.status = status
    job.save()
    JobStatusEvent.objects.create(job=job, status=status, actor=actor, note=note)


def add_entry(job, entry_type, amount, actor=None, **extra):
    return LedgerEntry.objects.create(
        job=job, entry_type=entry_type, amount=money(amount), created_by=actor, **extra)


def ledger_total(job, *entry_types):
    total = job.ledger.filter(entry_type__in=entry_types).aggregate(t=Sum('amount'))['t']
    return total or Decimal('0.00')


def homeowner_job_url(job):
    return reverse('storefront:job_detail', args=[job.pk])


def partner_job_url(job):
    return reverse('partner:job_detail', args=[job.pk])


# ---------------------------------------------------------------------------
# H05, H06: choosing a slot and the recommended handyman
# ---------------------------------------------------------------------------

@transaction.atomic
def choose_slot(job, slot_start, ranges, model_version, actor):
    """Save the chosen slot and its predicted range on the job, then rank handymen (H05).

    The saved prediction is used by every later decision, so the range the
    homeowner accepted cannot change during the job.
    """
    band = timing_band_for(slot_start)
    price = ranges[band]
    job.slot_start = slot_start
    job.slot_end = slot_start + timedelta(hours=settings.SLOT_HOURS)
    job.timing_band = band
    job.planning_region = job.address.planning_region
    job.price_p10, job.price_p50, job.price_p90 = price.low, price.mid, price.high
    job.model_version = model_version
    job.requested_at = timezone.now()
    set_status(job, Status.REQUESTED, actor,
               f'Slot chosen: {timezone.localtime(slot_start):%a %d %b, %I:%M %p} ({band})')
    return create_recommendations(job)


def create_recommendations(job):
    """Score every eligible handyman free for the slot, best first (H06)."""
    job.recommendations.filter(status=Recommendation.Status.PROPOSED).update(
        status=Recommendation.Status.UNAVAILABLE, decided_at=timezone.now())
    candidates = [h for h in eligible_handymen(job.job_type, job.tier) if is_free(h, job.slot_start)]
    scored = sorted(((recommendation_score(h, job.job_type), h) for h in candidates),
                    key=lambda pair: pair[0], reverse=True)
    start_rank = job.recommendations.count()
    Recommendation.objects.bulk_create([
        Recommendation(job=job, handyman=h, score=score, rank=start_rank + i + 1)
        for i, (score, h) in enumerate(scored)
    ])
    return len(scored)


def current_recommendation(job):
    """The highest-ranked proposed handyman who is still free for the slot, or None."""
    for rec in job.recommendations.filter(status=Recommendation.Status.PROPOSED).select_related(
            'handyman__user'):
        if is_free(rec.handyman, job.slot_start):
            return rec
        rec.status = Recommendation.Status.UNAVAILABLE
        rec.decided_at = timezone.now()
        rec.save()
    return None


def reset_to_slot_choice(job, actor=None):
    """No handyman left: ask the homeowner to pick another slot or tier (H06)."""
    job.slot_start = job.slot_end = None
    job.handyman = None
    job.quote_deadline = None
    set_status(job, Status.DRAFT, actor, 'No handyman left for this slot. Please choose another slot or tier.')


@transaction.atomic
def accept_recommendation(job, rec, actor):
    """Homeowner accepts; the job goes to the handyman, who has 4 hours to quote (P04)."""
    now = timezone.now()
    rec.status = Recommendation.Status.ACCEPTED
    rec.decided_at = now
    rec.save()
    job.handyman = rec.handyman
    job.matched_at = job.matched_at or now
    job.quote_deadline = now + timedelta(hours=settings.QUOTE_DEADLINE_HOURS)
    set_status(job, Status.HANDYMAN_ASSIGNED, actor, f'{rec.handyman.user.get_full_name()} assigned')
    notify(rec.handyman.user, f'New job request: {job.job_type}. Please quote within '
           f'{settings.QUOTE_DEADLINE_HOURS} hours.', partner_job_url(job))


def decline_recommendation(rec):
    rec.status = Recommendation.Status.HOMEOWNER_DECLINED
    rec.decided_at = timezone.now()
    rec.save()


@transaction.atomic
def release_handyman(job, rec_status, reason, actor=None):
    """Take the job off its handyman and pass it to the next recommendation (P04, H07)."""
    rec = job.recommendations.filter(handyman=job.handyman, status=Recommendation.Status.ACCEPTED).first()
    if rec:
        rec.status = rec_status
        rec.decline_reason = reason
        rec.decided_at = timezone.now()
        rec.save()
    job.handyman = None
    job.quote_deadline = None
    set_status(job, Status.REQUESTED, actor, reason)
    notify(job.homeowner, f'Job #{job.pk}: {reason}. Please review the next recommended handyman.',
           reverse('storefront:recommendation', args=[job.pk]))


# ---------------------------------------------------------------------------
# P05, A06, H07: quotations
# ---------------------------------------------------------------------------

@transaction.atomic
def submit_quotation(job, amount, work_description, justification, actor):
    """Release a quotation inside the predicted range at once; queue one outside it (P05)."""
    inside = is_within_range(job, amount)
    quotation = Quotation.objects.create(
        job=job, handyman=job.handyman, amount=amount, work_description=work_description,
        justification=justification, auto_released=inside,
        status=Quotation.Status.RELEASED if inside else Quotation.Status.UNDER_REVIEW,
    )
    job.quote_deadline = None
    if inside:
        set_status(job, Status.AWAITING_PAYMENT, actor, 'Quotation released automatically (within predicted range)')
        notify(job.homeowner, f'Your quotation for job #{job.pk} is ready.', homeowner_job_url(job))
    else:
        set_status(job, Status.QUOTATION_UNDER_REVIEW, actor, 'Quotation outside predicted range; sent for review')
        notify_staff(f'Quotation for job #{job.pk} needs review.', reverse('adminpanel:quotation_detail',
                                                                            args=[quotation.pk]))
    return quotation


@transaction.atomic
def release_quotation(quotation, employee, note=''):
    quotation.status = Quotation.Status.RELEASED
    quotation.reviewed_by = employee
    quotation.reviewed_at = timezone.now()
    quotation.review_note = note
    quotation.save()
    job = quotation.job
    set_status(job, Status.AWAITING_PAYMENT, employee, 'Quotation released by FixItFast staff')
    notify(job.homeowner, f'Your quotation for job #{job.pk} is ready.', homeowner_job_url(job))


@transaction.atomic
def return_quotation(quotation, employee, reason):
    quotation.status = Quotation.Status.RETURNED
    quotation.reviewed_by = employee
    quotation.reviewed_at = timezone.now()
    quotation.review_note = reason
    quotation.save()
    job = quotation.job
    job.quote_deadline = timezone.now() + timedelta(hours=settings.QUOTE_DEADLINE_HOURS)
    set_status(job, Status.QUOTATION_PENDING, employee, 'Quotation returned to handyman for revision')
    notify(job.handyman.user, f'Your quotation for job #{job.pk} was returned: {reason}', partner_job_url(job))


def service_fee_for(job):
    """(fee, waived). The fee is waived on a homeowner's first completed job (Section 1.7)."""
    others = Job.objects.filter(homeowner=job.homeowner).exclude(pk=job.pk)
    has_completed = others.filter(status=Status.COMPLETED).exists()
    waiver_in_use = others.filter(service_fee_waived=True).exclude(
        status__in=[Status.CANCELLED, Status.DECLINED]).exists()
    if not has_completed and not waiver_in_use:
        return Decimal('0.00'), True
    return settings.SERVICE_FEE, False


@transaction.atomic
def pay_quotation(job, quotation, actor):
    """Simulated payment: the quotation and service fee are held until completion (H07)."""
    fee, waived = service_fee_for(job)
    add_entry(job, Entry.QUOTATION_HELD, quotation.amount, actor, note='Held until the job is complete')
    if waived:
        add_entry(job, Entry.SERVICE_FEE_WAIVED, settings.SERVICE_FEE, actor, note='First-job waiver')
    else:
        add_entry(job, Entry.SERVICE_FEE, fee, actor)
    quotation.status = Quotation.Status.ACCEPTED
    quotation.save()
    job.service_fee_waived = waived
    set_status(job, Status.SCHEDULED, actor, 'Payment received and held')
    notify(job.handyman.user, f'Job #{job.pk} is confirmed for '
           f'{timezone.localtime(job.slot_start):%a %d %b, %I:%M %p}.', partner_job_url(job))


@transaction.atomic
def reject_quotation(job, quotation, actor):
    quotation.status = Quotation.Status.REJECTED
    quotation.save()
    release_handyman(job, Recommendation.Status.QUOTE_REJECTED, 'Quotation rejected by homeowner', actor)


# ---------------------------------------------------------------------------
# H08, A10: cancelling and refunds
# ---------------------------------------------------------------------------

def refund_all(job, actor, note):
    """Refund everything the homeowner has paid on the job that is not yet refunded."""
    paid = ledger_total(job, Entry.QUOTATION_HELD, Entry.SERVICE_FEE, Entry.PART_PAYMENT)
    outstanding = paid - ledger_total(job, Entry.REFUND)
    if outstanding > 0:
        add_entry(job, Entry.REFUND, outstanding, actor, note=note)


def close_open_reviews(job, note):
    """When a job ends early, take it out of the staff queues (A06, A07).

    Otherwise an employee could later release a quotation and revive the job,
    or reject a part receipt and refund a part that was already refunded.
    """
    job.quotations.filter(status__in=[Quotation.Status.UNDER_REVIEW, Quotation.Status.RELEASED]).update(
        status=Quotation.Status.REJECTED, review_note=note)
    job.parts.filter(review_status=PartLine.ReviewStatus.PENDING).update(
        review_status=PartLine.ReviewStatus.NOT_REQUIRED, review_reason=note)


@transaction.atomic
def cancel_job(job, actor, reason):
    """Full refund, the handyman is told and the slot is freed (H08, A10)."""
    refund_all(job, actor, 'Full refund on cancellation')
    close_open_reviews(job, 'Job cancelled and fully refunded')
    job.cancellation_reason = reason
    set_status(job, Status.CANCELLED, actor, reason)
    if job.handyman:
        notify(job.handyman.user, f'Job #{job.pk} was cancelled: {reason}', partner_job_url(job))
    if actor != job.homeowner:
        notify(job.homeowner, f'Job #{job.pk} was cancelled and fully refunded: {reason}', homeowner_job_url(job))


# ---------------------------------------------------------------------------
# P06, P07, H09: the visit
# ---------------------------------------------------------------------------

NEXT_PROGRESS = {
    Status.SCHEDULED: Status.ON_THE_WAY,
    Status.ON_THE_WAY: Status.IN_PROGRESS,
}


def advance_progress(job, actor):
    new_status = NEXT_PROGRESS[job.status]
    set_status(job, new_status, actor)
    notify(job.homeowner, f'Job #{job.pk} is now {new_status.label}.', homeowner_job_url(job))


def completion_blockers(job):
    """Reasons the handyman cannot mark the job Completed yet (P06)."""
    problems = []
    kinds = set(job.photos.values_list('kind', flat=True))
    if 'BEFORE' not in kinds:
        problems.append('Upload at least one photo from before the work.')
    if 'AFTER' not in kinds:
        problems.append('Upload at least one photo from after the work.')
    if job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.PENDING).exists():
        problems.append('Wait for the homeowner to approve or decline the added part.')
    return problems


def mark_completed(job, actor):
    job.completed_at = timezone.now()
    set_status(job, Status.COMPLETED, actor, 'Handyman marked the job completed')
    notify(job.homeowner, f'Job #{job.pk} is done. Please confirm and rate your handyman.',
           reverse('storefront:confirm_job', args=[job.pk]))


def approved_parts_total(job):
    parts = job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.APPROVED).exclude(
        review_status=PartLine.ReviewStatus.REJECTED)
    return parts.aggregate(t=Sum('price'))['t'] or Decimal('0.00')


def job_total(job):
    """Quotation plus approved parts: the repair amount the model's range is compared with."""
    quotation = job.accepted_quotation
    return (quotation.amount if quotation else Decimal('0')) + approved_parts_total(job)


@transaction.atomic
def add_part(job, name, price, receipt, actor):
    """Flag the part when the revised total exceeds the saved 90th percentile (P07)."""
    revised_total = job_total(job) + price
    flagged = exceeds_upper_range(job, revised_total)
    part = PartLine.objects.create(
        job=job, name=name, price=price, receipt=receipt, flagged=flagged,
        review_status=PartLine.ReviewStatus.PENDING if flagged else PartLine.ReviewStatus.NOT_REQUIRED,
    )
    JobStatusEvent.objects.create(job=job, status=job.status, actor=actor,
                                  note=f'Part added: {name} (S${price})')
    notify(job.homeowner, f'Your handyman added a part to job #{job.pk}: {name}, S${price}. '
           f'Please approve or decline.', reverse('storefront:part_decision', args=[part.pk]))
    return part


@transaction.atomic
def approve_part(part, actor):
    """Homeowner pays the difference (H09). A flagged part then goes to staff (A07)."""
    part.homeowner_decision = PartLine.HomeownerDecision.APPROVED
    part.save()
    job = part.job
    add_entry(job, Entry.PART_PAYMENT, part.price, actor, part=part, note=part.name)
    JobStatusEvent.objects.create(job=job, status=job.status, actor=actor, note=f'Part approved: {part.name}')
    notify(job.handyman.user, f'The homeowner approved {part.name} on job #{job.pk}.', partner_job_url(job))
    if part.flagged:
        notify_staff(f'Part on job #{job.pk} is above the predicted range; receipt check needed.',
                     reverse('adminpanel:part_detail', args=[part.pk]))


@transaction.atomic
def decline_repair(job, part, actor):
    """Decline after the visit started: refund all, charge the S$25 consultation fee (H09, Table 2)."""
    job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.PENDING).update(
        homeowner_decision=PartLine.HomeownerDecision.DECLINED)
    refund_all(job, actor, 'Quotation, service fee and parts refunded on decline')
    close_open_reviews(job, 'Repair declined; parts already refunded')
    fee = settings.CONSULTATION_FEE
    rate = commission_rate(job.handyman)
    commission = money(fee * rate)
    add_entry(job, Entry.CONSULTATION_FEE, fee, actor)
    add_entry(job, Entry.COMMISSION, commission, actor, commission_rate=rate, note='On consultation fee')
    add_entry(job, Entry.PAYOUT, fee - commission, actor, note='Consultation fee less commission')
    set_status(job, Status.DECLINED, actor, f'Homeowner declined the repair after part "{part.name}"')
    notify(job.handyman.user, f'The homeowner declined the repair on job #{job.pk}. '
           f'You will be paid the consultation fee less commission.', partner_job_url(job))


# ---------------------------------------------------------------------------
# H10: confirmation, payout and rating
# ---------------------------------------------------------------------------

@transaction.atomic
def confirm_completion(job, actor=None, auto=False):
    """Release the payout (except parts still under review) and start the warranty (H10)."""
    now = timezone.now()
    rate = commission_rate(job.handyman)
    payable = job.accepted_quotation.amount
    held = []
    for part in job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.APPROVED):
        if part.review_status == PartLine.ReviewStatus.PENDING:
            held.append(part)
        elif part.review_status != PartLine.ReviewStatus.REJECTED:
            payable += part.price

    commission = money(payable * rate)
    add_entry(job, Entry.COMMISSION, commission, actor, commission_rate=rate)
    add_entry(job, Entry.PAYOUT, payable - commission, actor, note='Repair amount less commission')
    for part in held:
        add_entry(job, Entry.PAYOUT_HELD, part.price, actor, part=part, note=f'{part.name}: receipt check pending')

    job.confirmed_at = now
    job.warranty_ends_at = now + timedelta(days=settings.WARRANTY_DAYS[job.tier])
    note = 'Confirmed automatically after 48 hours' if auto else 'Homeowner confirmed completion'
    set_status(job, Status.COMPLETED, actor, note)
    notify(job.handyman.user, f'Job #{job.pk} confirmed. Your payout has been released.', partner_job_url(job))


def report_problem(job, note, actor):
    job.problem_note = note
    job.save()
    JobStatusEvent.objects.create(job=job, status=job.status, actor=actor, note=f'Problem reported: {note}')
    notify_staff(f'Homeowner reported a problem on job #{job.pk}.', reverse('adminpanel:job_detail', args=[job.pk]))


def submit_review(job, rating, comment):
    review = Review.objects.create(job=job, handyman=job.handyman, rating=rating, comment=comment)
    notify(job.handyman.user, f'You received a {rating}-star rating for job #{job.pk}.', partner_job_url(job))
    return review


# ---------------------------------------------------------------------------
# A07: flagged part review
# ---------------------------------------------------------------------------

@transaction.atomic
def review_part(part, employee, approve, reason=''):
    job = part.job
    part.review_status = PartLine.ReviewStatus.APPROVED if approve else PartLine.ReviewStatus.REJECTED
    part.reviewed_by = employee
    part.reviewed_at = timezone.now()
    part.review_reason = reason
    part.save()
    if approve:
        if job.confirmed_at:   # payout already settled without this part; release it now
            rate = commission_rate(job.handyman)
            commission = money(part.price * rate)
            add_entry(job, Entry.COMMISSION, commission, employee, commission_rate=rate, part=part)
            add_entry(job, Entry.PAYOUT, part.price - commission, employee, part=part,
                      note=f'{part.name}: released after receipt check')
        notify(job.handyman.user, f'Receipt for {part.name} on job #{job.pk} approved.', partner_job_url(job))
    else:
        add_entry(job, Entry.REFUND, part.price, employee, part=part, note=f'{part.name}: receipt rejected')
        notify(job.handyman.user, f'Receipt for {part.name} on job #{job.pk} rejected: {reason}', partner_job_url(job))
        notify(job.homeowner, f'S${part.price} for {part.name} on job #{job.pk} has been refunded to you.',
               homeowner_job_url(job))


# ---------------------------------------------------------------------------
# H11, A08: warranty claims
# ---------------------------------------------------------------------------

def can_claim(job):
    return job.warranty_active and not job.claims.exclude(
        status__in=[WarrantyClaim.Status.RESOLVED, WarrantyClaim.Status.REJECTED]).exists()


@transaction.atomic
def raise_claim(job, description, photos):
    claim = WarrantyClaim.objects.create(job=job, description=description)
    for image in photos:
        ClaimPhoto.objects.create(claim=claim, image=image)
    notify_staff(f'New warranty claim on job #{job.pk}.', reverse('adminpanel:claim_detail', args=[claim.pk]))
    return claim


def original_available_soon(job):
    """Platinum remedy: is the original handyman free within three days? (A08)"""
    start = timezone.localdate()
    return any(True for _ in free_slots(job.handyman, start, settings.REPLACEMENT_HANDYMAN_DAYS + 1))


def update_claim(claim, employee, status, note='', **fields):
    for name, value in fields.items():
        setattr(claim, name, value)
    claim.status = status
    claim.handled_by = employee
    if note:
        claim.resolution_note = note
    claim.save()
    notify(claim.job.homeowner, f'Warranty claim on job #{claim.job.pk}: {claim.get_status_display()}.',
           homeowner_job_url(claim.job))


@transaction.atomic
def refund_claim(claim, employee, amount, note):
    """Standard remedy: full or partial refund."""
    add_entry(claim.job, Entry.REFUND, amount, employee, note=f'Warranty refund: {note}')
    update_claim(claim, employee, WarrantyClaim.Status.RESOLVED, note, refund_amount=amount)


def schedule_return_visit(claim, employee, when, replacement=None, note=''):
    """Gold and Platinum remedy: a free return visit, with a replacement at Platinum if needed."""
    update_claim(claim, employee, WarrantyClaim.Status.RETURN_VISIT_SCHEDULED, note,
                 return_visit_at=when, replacement_handyman=replacement)
    handyman = replacement or claim.job.handyman
    notify(handyman.user, f'Warranty return visit for job #{claim.job.pk} on '
           f'{timezone.localtime(when):%a %d %b, %I:%M %p}.', partner_job_url(claim.job))


# ---------------------------------------------------------------------------
# A10, H08: payment summary for the ledger and receipt
# ---------------------------------------------------------------------------

def payment_summary(job):
    quotation = ledger_total(job, Entry.QUOTATION_HELD)
    parts = ledger_total(job, Entry.PART_PAYMENT)
    service_fee = ledger_total(job, Entry.SERVICE_FEE)
    consultation = ledger_total(job, Entry.CONSULTATION_FEE)
    refunds = ledger_total(job, Entry.REFUND)
    commission = ledger_total(job, Entry.COMMISSION)
    fee_kept = service_fee if job.status not in (Status.CANCELLED, Status.DECLINED) else Decimal('0.00')
    held = job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.APPROVED, flagged=True,
                            review_status=PartLine.ReviewStatus.PENDING).aggregate(t=Sum('price'))['t']
    return {
        'quotation': quotation,
        'parts': parts,
        'service_fee': service_fee,
        'fee_waived': ledger_total(job, Entry.SERVICE_FEE_WAIVED),
        'consultation_fee': consultation,
        'refunds': refunds,
        'paid_in': quotation + parts + service_fee,
        'net_paid': quotation + parts + service_fee + consultation - refunds,
        'commission': commission,
        'payout': ledger_total(job, Entry.PAYOUT),
        'held': held or Decimal('0.00'),
        'platform_revenue': commission + fee_kept,
    }


# ---------------------------------------------------------------------------
# Time-based rules, run by jobs.middleware.HousekeepingMiddleware
# ---------------------------------------------------------------------------

def housekeeping():
    now = timezone.now()
    # P04: the handyman must quote within four hours or the job moves on.
    overdue = Job.objects.filter(status__in=[Status.HANDYMAN_ASSIGNED, Status.QUOTATION_PENDING],
                                 quote_deadline__lt=now)
    for job in overdue:
        release_handyman(job, Recommendation.Status.EXPIRED, 'The handyman did not quote in time')
    # H10: confirm automatically 48 hours after the handyman marks the job Completed.
    cutoff = now - timedelta(hours=settings.AUTO_CONFIRM_HOURS)
    for job in Job.objects.filter(status=Status.COMPLETED, confirmed_at__isnull=True,
                                  completed_at__lt=cutoff, problem_note=''):
        confirm_completion(job, auto=True)
