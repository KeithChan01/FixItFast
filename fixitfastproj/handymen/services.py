"""Handyman availability, scoring and tier rules (P02, H05, H06, A05)."""
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Avg
from django.utils import timezone

from catalogue.models import Tier, TimingBand
from handymen.models import Document, HandymanProfile
from jobs.models import Job, Recommendation

# Two-hour slots that never straddle the 6 p.m. peak boundary: four off-peak
# weekday slots (9-11, 11-1, 2-4, 4-6, with a lunch break) and two evening slots.
SLOT_START_TIMES = [datetime.strptime(f'{h}:00', '%H:%M').time() for h in (9, 11, 14, 16, 18, 20)]


# ---------------------------------------------------------------------------
# Timing bands and availability
# ---------------------------------------------------------------------------

def timing_band_for(slot_start):
    """Off-peak is weekdays 9 a.m. to 6 p.m. Everything else is peak (H05)."""
    local = timezone.localtime(slot_start)
    if local.weekday() < 5 and 9 <= local.hour < 18:
        return TimingBand.OFF_PEAK
    return TimingBand.PEAK


def eligible_handymen(job_type, tier):
    """Approved, active handymen in exactly the selected tier who do this job type."""
    return HandymanProfile.objects.filter(
        application_status=HandymanProfile.ApplicationStatus.APPROVED,
        is_suspended=False,
        user__is_active=True,
        tier=tier,
        job_types=job_type,
    ).select_related('user')


def free_slots(handyman, start_date, days):
    """Yield the handyman's free slot start times (aware datetimes) over `days` days.

    A slot is free when it is in the weekly template, not blocked by a leave
    exception, in the future, and not already taken by an active job (P02).
    """
    weekly = {}
    for slot in handyman.weekly_slots.all():
        weekly.setdefault(slot.weekday, []).append(slot.start_time)

    end_date = start_date + timedelta(days=days)
    whole_days, blocked = set(), set()
    for exc in handyman.exceptions.filter(date__gte=start_date, date__lt=end_date):
        if exc.start_time is None:
            whole_days.add(exc.date)
        else:
            blocked.add((exc.date, exc.start_time))

    booked = set(handyman.jobs.filter(status__in=Job.ACTIVE_STATUSES, slot_start__isnull=False)
                 .values_list('slot_start', flat=True))
    now = timezone.now()

    for offset in range(days):
        day = start_date + timedelta(days=offset)
        if day in whole_days:
            continue
        for start_time in sorted(weekly.get(day.weekday(), [])):
            if (day, start_time) in blocked:
                continue
            start = timezone.make_aware(datetime.combine(day, start_time))
            if start > now and start not in booked:
                yield start


def is_free(handyman, slot_start):
    day = timezone.localtime(slot_start).date()
    return slot_start in set(free_slots(handyman, day, 1))


def slot_calendar(job_type, tier):
    """{slot_start: [handymen free then]} over the booking window, starting tomorrow (H05)."""
    start_date = timezone.localdate() + timedelta(days=1)
    calendar = {}
    for handyman in eligible_handymen(job_type, tier).prefetch_related('weekly_slots'):
        for start in free_slots(handyman, start_date, settings.SLOT_BOOKING_DAYS):
            calendar.setdefault(start, []).append(handyman)
    return dict(sorted(calendar.items()))


# ---------------------------------------------------------------------------
# Track record
# ---------------------------------------------------------------------------

def completed_jobs(handyman):
    return handyman.jobs.filter(status=Job.Status.COMPLETED, confirmed_at__isnull=False)


def average_rating(handyman, last=None):
    reviews = handyman.reviews.order_by('-created_at')
    if last:
        reviews = handyman.reviews.filter(pk__in=list(reviews.values_list('pk', flat=True)[:last]))
    value = reviews.aggregate(avg=Avg('rating'))['avg']
    return Decimal(str(round(value, 2))) if value is not None else None


def recent_cancellations(handyman, days=90):
    """Jobs the handyman declined or let expire in the last 90 days."""
    since = timezone.now() - timedelta(days=days)
    return handyman.recommendations.filter(
        status__in=[Recommendation.Status.HANDYMAN_DECLINED, Recommendation.Status.EXPIRED],
        decided_at__gte=since,
    ).count()


def commission_rate(handyman):
    """10% on a handyman's first 20 jobs (Section 1.7), 15% after that."""
    if completed_jobs(handyman).count() < settings.EARLY_COMMISSION_JOB_COUNT:
        return settings.EARLY_COMMISSION_RATE
    return settings.COMMISSION_RATE


def valid_documents(handyman, doc_type):
    """Approved documents of a type that have not expired."""
    today = timezone.localdate()
    return handyman.documents.filter(status=Document.Status.APPROVED, doc_type=doc_type).exclude(
        expiry_date__lt=today)


def holds_licence_for(handyman, job_type):
    return valid_documents(handyman, Document.DocType.LICENCE).filter(job_types=job_type).exists()


def allowed_job_types(handyman, queryset):
    """Job types at or below the handyman's tier; licensed ones only with a verified licence (P02)."""
    allowed = []
    for job_type in queryset.filter(min_tier__in=Tier.at_or_below(handyman.tier)):
        if not job_type.licence_required or holds_licence_for(handyman, job_type):
            allowed.append(job_type.pk)
    return queryset.filter(pk__in=allowed)


# ---------------------------------------------------------------------------
# Recommendation score (H06). Rule-based, not part of the ML model.
# ---------------------------------------------------------------------------

def recommendation_score(handyman, job_type):
    """Score from 0 to 1 on skill match, rating, experience and recent cancellations."""
    certified = Document.objects.filter(
        handyman=handyman, status=Document.Status.APPROVED,
        doc_type__in=[Document.DocType.CERTIFICATE, Document.DocType.LICENCE],
        job_types=job_type,
    ).exists()
    skill = 1.0 if certified else 0.6
    rating = float(average_rating(handyman) or 4)       # unrated handymen get a neutral 4.0
    experience = min(completed_jobs(handyman).count() / settings.PLATINUM_MIN_JOBS, 1.0)
    cancellations = min(recent_cancellations(handyman), 5) / 5

    score = 0.35 * skill + 0.35 * (rating / 5) + 0.20 * experience + 0.10 * (1 - cancellations)
    return Decimal(str(round(score, 3)))


# ---------------------------------------------------------------------------
# Tier eligibility (A05)
# ---------------------------------------------------------------------------

def tier_requirements(handyman):
    """List of (requirement, met) for Gold and Platinum, shown on the handyman record."""
    done = completed_jobs(handyman).count()
    gold_rating = average_rating(handyman, last=settings.GOLD_RATING_WINDOW)
    overall_rating = average_rating(handyman)
    has_certificate = valid_documents(handyman, Document.DocType.CERTIFICATE).exists()
    has_insurance = valid_documents(handyman, Document.DocType.INSURANCE).exists()
    passed_quiz = handyman.quiz_attempts.filter(passed=True).exists()

    gold = [
        (f'At least {settings.GOLD_MIN_JOBS} completed jobs ({done})', done >= settings.GOLD_MIN_JOBS),
        (f'Average rating {settings.GOLD_MIN_RATING}+ over last {settings.GOLD_RATING_WINDOW} '
         f'({gold_rating or "none"})', bool(gold_rating and gold_rating >= settings.GOLD_MIN_RATING)),
        ('Verified relevant certificate', has_certificate),
    ]
    platinum = [
        (f'At least {settings.PLATINUM_MIN_JOBS} completed jobs ({done})', done >= settings.PLATINUM_MIN_JOBS),
        (f'Average rating {settings.PLATINUM_MIN_RATING}+ ({overall_rating or "none"})',
         bool(overall_rating and overall_rating >= settings.PLATINUM_MIN_RATING)),
        ('Passed FixItFast training quiz', passed_quiz),
        ('Valid public liability insurance', has_insurance),
    ]
    return {Tier.GOLD: gold, Tier.PLATINUM: platinum}


def meets_tier(handyman, tier, requirements=None):
    if tier == Tier.STANDARD:
        return True
    requirements = requirements or tier_requirements(handyman)
    met_gold = all(ok for _, ok in requirements[Tier.GOLD])
    if tier == Tier.GOLD:
        return met_gold
    return met_gold and all(ok for _, ok in requirements[Tier.PLATINUM])


def next_tier(tier):
    rank = Tier.rank(tier)
    return Tier.values[rank + 1] if rank + 1 < len(Tier.values) else None


def eligible_for_upgrade(handyman, requirements=None):
    upgrade = next_tier(handyman.tier)
    return upgrade if upgrade and meets_tier(handyman, upgrade, requirements) else None
