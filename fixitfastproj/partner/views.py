"""Handyman partner section: user stories P01 to P07."""
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.db import transaction
from django.db.models import Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import approved_handyman_required, handyman_required
from accounts.forms import ReasonForm
from accounts.models import User
from accounts.services import notify_staff
from catalogue.models import JobType, Tier
from handymen.models import Document, HandymanProfile, TrainingModule, QuizQuestion, WeeklyAvailability
from handymen.services import SLOT_START_TIMES, allowed_job_types, average_rating, completed_jobs
from jobs import services
from jobs.models import Job, JobPhoto, LedgerEntry, Recommendation

from .forms import (
    ApplicationForm, DocumentForm, ExceptionForm, HandymanProfileForm, JobPhotoForm, PartForm,
    QuizForm, QuotationForm,
)

Status = Job.Status
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


# ---------------------------------------------------------------------------
# P01: apply as a handyman
# ---------------------------------------------------------------------------

def apply(request):
    if request.user.is_authenticated and request.user.role == User.Role.HANDYMAN:
        return redirect('partner:home')
    form = ApplicationForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            user = form.save(role=User.Role.HANDYMAN)
            profile = HandymanProfile.objects.create(user=user, years_experience=data['years_experience'])
            profile.job_types.set(data['job_types'])
            Document.objects.create(handyman=profile, doc_type=Document.DocType.IDENTITY,
                                    title='Identity document', file=data['identity_document'])
            for name, doc_type in (('certificate', Document.DocType.CERTIFICATE),
                                   ('licence', Document.DocType.LICENCE)):
                if data.get(name):
                    doc = Document.objects.create(handyman=profile, doc_type=doc_type, title=data[f'{name}_title'],
                                                  file=data[name], expiry_date=data.get(f'{name}_expiry'))
                    doc.job_types.set(data['job_types'])
            if data.get('insurance'):
                Document.objects.create(handyman=profile, doc_type=Document.DocType.INSURANCE,
                                        title='Public liability insurance', file=data['insurance'],
                                        expiry_date=data.get('insurance_expiry'))
        notify_staff(f'New handyman application from {user.get_full_name()}.')
        login(request, user)
        messages.success(request, 'Application submitted. We will verify your documents shortly.')
        return redirect('partner:home')
    return render(request, 'partner/apply.html', {'form': form})


@handyman_required
def home(request):
    """Application status for new applicants; a dashboard once approved."""
    profile = request.user.handyman
    if profile.application_status != HandymanProfile.ApplicationStatus.APPROVED:
        return render(request, 'partner/application_status.html', {
            'profile': profile, 'documents': profile.documents.all(), 'form': DocumentForm(),
        })

    jobs = profile.jobs.select_related('job_type')
    month_start = timezone.localdate().replace(day=1)
    payouts = LedgerEntry.objects.filter(job__handyman=profile, entry_type=LedgerEntry.EntryType.PAYOUT,
                                         created_at__date__gte=month_start).aggregate(t=Sum('amount'))['t']
    return render(request, 'partner/home.html', {
        'profile': profile,
        'requests': jobs.filter(status__in=[Status.HANDYMAN_ASSIGNED, Status.QUOTATION_PENDING]),
        'upcoming': jobs.filter(status__in=[Status.SCHEDULED, Status.ON_THE_WAY, Status.IN_PROGRESS]).order_by('slot_start'),
        'awaiting': jobs.filter(status__in=[Status.QUOTATION_UNDER_REVIEW, Status.AWAITING_PAYMENT]),
        'rating': average_rating(profile),
        'completed': completed_jobs(profile).count(),
        'month_payouts': payouts or 0,
    })


@require_POST
@handyman_required
def resubmit(request):
    """P01: a rejected applicant uploads new documents and resubmits."""
    profile = request.user.handyman
    form = DocumentForm(request.POST, request.FILES)
    if form.is_valid():
        doc = form.save(commit=False)
        doc.handyman = profile
        doc.save()
        form.save_m2m()
        messages.success(request, 'Document uploaded.')
    else:
        messages.error(request, 'Please check the document form: ' + ' '.join(
            e for errors in form.errors.values() for e in errors))
    if 'resubmit' in request.POST and profile.application_status == HandymanProfile.ApplicationStatus.REJECTED:
        profile.application_status = HandymanProfile.ApplicationStatus.PENDING
        profile.rejection_reason = ''
        profile.save()
        notify_staff(f'{request.user.get_full_name()} resubmitted their application.')
        messages.success(request, 'Application resubmitted.')
    return redirect('partner:home')


# ---------------------------------------------------------------------------
# P02: profile, job types, documents and availability
# ---------------------------------------------------------------------------

@approved_handyman_required
def profile(request):
    handyman = request.user.handyman
    allowed = allowed_job_types(handyman, JobType.objects.filter(is_active=True))
    profile_form = HandymanProfileForm(
        request.POST if 'save_profile' in request.POST else None,
        request.FILES if 'save_profile' in request.POST else None,
        instance=handyman, allowed_job_types=allowed)
    document_form = DocumentForm(request.POST if 'upload_document' in request.POST else None,
                                 request.FILES if 'upload_document' in request.POST else None)

    if request.method == 'POST':
        if 'save_profile' in request.POST and profile_form.is_valid():
            profile_form.save()
            messages.success(request, 'Profile updated.')
            return redirect('partner:profile')
        if 'upload_document' in request.POST and document_form.is_valid():
            doc = document_form.save(commit=False)
            doc.handyman = handyman
            doc.save()
            document_form.save_m2m()
            notify_staff(f'{request.user.get_full_name()} uploaded a new document for verification.')
            messages.success(request, 'Document sent for verification.')
            return redirect('partner:profile')

    locked = JobType.objects.filter(is_active=True).exclude(pk__in=allowed)
    return render(request, 'partner/profile.html', {
        'profile_form': profile_form, 'document_form': document_form, 'handyman': handyman,
        'documents': handyman.documents.all(), 'locked_job_types': locked,
    })


@approved_handyman_required
def availability(request):
    handyman = request.user.handyman
    exception_form = ExceptionForm(request.POST if 'add_exception' in request.POST else None)

    if request.method == 'POST':
        if 'save_weekly' in request.POST:
            # Each checkbox is named "slot-<weekday>-<HH:MM>".
            chosen = set(request.POST.getlist('slots'))
            with transaction.atomic():
                handyman.weekly_slots.all().delete()
                WeeklyAvailability.objects.bulk_create([
                    WeeklyAvailability(handyman=handyman, weekday=weekday, start_time=start)
                    for weekday in range(7) for start in SLOT_START_TIMES
                    if f'{weekday}-{start:%H:%M}' in chosen
                ])
            messages.success(request, 'Weekly availability saved.')
            return redirect('partner:availability')
        if 'add_exception' in request.POST and exception_form.is_valid():
            exc = exception_form.save(commit=False)
            exc.handyman = handyman
            exc.save()
            messages.success(request, 'Time off added.')
            return redirect('partner:availability')
        if 'delete_exception' in request.POST:
            handyman.exceptions.filter(pk=request.POST['delete_exception']).delete()
            return redirect('partner:availability')

    current = {f'{s.weekday}-{s.start_time:%H:%M}' for s in handyman.weekly_slots.all()}
    grid = [{
        'time': start,
        'end': (datetime.combine(date.today(), start) + timedelta(hours=settings.SLOT_HOURS)).time(),
        'cells': [{
            'value': f'{weekday}-{start:%H:%M}',
            'checked': f'{weekday}-{start:%H:%M}' in current,
            # Same rule as handymen.services.timing_band_for, shown so handymen see which slots are peak.
            'peak': not (weekday < 5 and 9 <= start.hour < 18),
        } for weekday in range(7)],
    } for start in SLOT_START_TIMES]
    return render(request, 'partner/availability.html', {
        'grid': grid, 'weekdays': WEEKDAYS, 'exception_form': exception_form,
        'exceptions': handyman.exceptions.filter(date__gte=timezone.localdate()),
        'booked': handyman.jobs.filter(status__in=Job.ACTIVE_STATUSES, slot_start__gte=timezone.now())
                               .order_by('slot_start'),
    })


# ---------------------------------------------------------------------------
# P03: Platinum training
# ---------------------------------------------------------------------------

def quiz_retake_at(handyman):
    """When a failed quiz can next be taken, or None if it can be taken now."""
    last = handyman.quiz_attempts.first()
    if last and not last.passed:
        retake = last.taken_at + timedelta(hours=settings.QUIZ_RETAKE_HOURS)
        if retake > timezone.now():
            return retake
    return None


@approved_handyman_required
def training(request):
    handyman = request.user.handyman
    if handyman.tier != Tier.GOLD and not handyman.quiz_attempts.exists():
        messages.info(request, 'Platinum training is available to Gold handymen.')
        return redirect('partner:home')
    return render(request, 'partner/training.html', {
        'modules': TrainingModule.objects.all(),
        'attempts': handyman.quiz_attempts.all()[:5],
        'passed': handyman.quiz_attempts.filter(passed=True).exists(),
        'retake_at': quiz_retake_at(handyman),
    })


@approved_handyman_required
def quiz(request):
    handyman = request.user.handyman
    if handyman.tier != Tier.GOLD or handyman.quiz_attempts.filter(passed=True).exists():
        return redirect('partner:training')
    if quiz_retake_at(handyman):
        messages.warning(request, 'You can retake the quiz 24 hours after your last attempt.')
        return redirect('partner:training')

    form = QuizForm(request.POST or None, questions=QuizQuestion.objects.select_related('module'))
    if request.method == 'POST' and form.is_valid():
        correct, total = form.score()
        score = round(correct / total, 3)
        passed = score >= settings.QUIZ_PASS_MARK
        handyman.quiz_attempts.create(score=score, passed=passed)
        if passed:
            messages.success(request, f'You passed with {correct}/{total}. This counts towards Platinum.')
        else:
            messages.error(request, f'You scored {correct}/{total}. The pass mark is 80%. Try again in 24 hours.')
        return redirect('partner:training')
    return render(request, 'partner/quiz.html', {'form': form})


# ---------------------------------------------------------------------------
# P04 to P07: jobs
# ---------------------------------------------------------------------------

@approved_handyman_required
def jobs(request):
    handyman = request.user.handyman
    status = request.GET.get('status', 'open')
    all_jobs = handyman.jobs.select_related('job_type')
    if status == 'open':
        all_jobs = all_jobs.exclude(status__in=Job.CLOSED_STATUSES)
    elif status == 'closed':
        all_jobs = all_jobs.filter(status__in=Job.CLOSED_STATUSES)
    return render(request, 'partner/jobs.html', {'jobs': all_jobs.order_by('-slot_start'), 'status': status})


def handyman_job(request, pk):
    """A job assigned to this handyman, or a past one they worked on."""
    return get_object_or_404(Job.objects.select_related('job_type', 'address', 'homeowner'),
                             pk=pk, handyman=request.user.handyman)


def job_detail_context(job, quote_form=None):
    needs_quote = job.status in (Status.HANDYMAN_ASSIGNED, Status.QUOTATION_PENDING)
    in_progress = job.status == Status.IN_PROGRESS
    return {
        'job': job,
        'request_photos': job.photos.filter(kind=JobPhoto.Kind.REQUEST),
        'work_photos': job.photos.exclude(kind=JobPhoto.Kind.REQUEST),
        'quotations': job.quotations.filter(handyman=job.handyman),
        'parts': job.parts.all(),
        'needs_quote': needs_quote,
        'quote_form': quote_form or (QuotationForm(job=job) if needs_quote else None),
        'decline_form': ReasonForm() if needs_quote else None,
        'photo_form': JobPhotoForm() if in_progress else None,
        'part_form': PartForm() if in_progress else None,
        'blockers': services.completion_blockers(job) if in_progress else [],
        # The full address is only shown once the homeowner has paid (P04).
        'show_address': job.is_paid,
        'payouts': job.ledger.filter(entry_type__in=[
            LedgerEntry.EntryType.COMMISSION, LedgerEntry.EntryType.PAYOUT, LedgerEntry.EntryType.PAYOUT_HELD]),
        'claims': job.claims.select_related('replacement_handyman__user'),
        'commission_rate': services.commission_rate(job.handyman),
    }


@approved_handyman_required
def job_detail(request, pk):
    job = handyman_job(request, pk)
    return render(request, 'partner/job_detail.html', job_detail_context(job))


@require_POST
@approved_handyman_required
def decline_job(request, pk):
    """P04: decline with a reason; the job passes to the next handyman."""
    job = handyman_job(request, pk)
    form = ReasonForm(request.POST)
    if job.status in (Status.HANDYMAN_ASSIGNED, Status.QUOTATION_PENDING) and form.is_valid():
        services.release_handyman(job, Recommendation.Status.HANDYMAN_DECLINED,
                                  f'The handyman declined: {form.cleaned_data["reason"]}', request.user)
        messages.info(request, 'Job declined.')
        return redirect('partner:home')
    messages.error(request, 'Please give a reason for declining.')
    return redirect('partner:job_detail', pk=pk)


@approved_handyman_required
def submit_quote(request, pk):
    """P05 [ML]: inside the predicted range is released at once, outside goes to review."""
    job = handyman_job(request, pk)
    if job.status not in (Status.HANDYMAN_ASSIGNED, Status.QUOTATION_PENDING):
        return redirect('partner:job_detail', pk=pk)
    form = QuotationForm(request.POST or None, job=job)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        quote = services.submit_quotation(job, data['amount'], data['work_description'],
                                          data['justification'], request.user)
        if quote.auto_released:
            messages.success(request, 'Quotation is within the predicted range and has been sent to the homeowner.')
        else:
            messages.info(request, 'Quotation is outside the predicted range and has been sent to FixItFast for review.')
        return redirect('partner:job_detail', pk=pk)
    if request.method == 'GET':
        return redirect('partner:job_detail', pk=pk)
    # Re-show the job page with the form errors.
    return render(request, 'partner/job_detail.html', job_detail_context(job, quote_form=form))


@require_POST
@approved_handyman_required
def update_progress(request, pk):
    """P06: On the Way -> In Progress -> Completed."""
    job = handyman_job(request, pk)
    if job.status in services.NEXT_PROGRESS:
        services.advance_progress(job, request.user)
    elif job.status == Status.IN_PROGRESS:
        blockers = services.completion_blockers(job)
        if blockers:
            for problem in blockers:
                messages.error(request, problem)
        else:
            services.mark_completed(job, request.user)
            messages.success(request, 'Job marked completed. The homeowner has been asked to confirm.')
    return redirect('partner:job_detail', pk=pk)


@require_POST
@approved_handyman_required
def upload_photos(request, pk):
    job = handyman_job(request, pk)
    form = JobPhotoForm(request.POST, request.FILES)
    if job.status == Status.IN_PROGRESS and form.is_valid():
        for image in form.cleaned_data['images']:
            JobPhoto.objects.create(job=job, image=image, kind=form.cleaned_data['kind'])
        messages.success(request, 'Photos uploaded.')
    else:
        messages.error(request, 'Please choose at least one photo.')
    return redirect('partner:job_detail', pk=pk)


@require_POST
@approved_handyman_required
def add_part(request, pk):
    """P07 [ML]: a part that takes the total above the 90th percentile is flagged for review."""
    job = handyman_job(request, pk)
    if job.status != Status.IN_PROGRESS:
        return redirect('partner:job_detail', pk=pk)
    form = PartForm(request.POST, request.FILES)
    if form.is_valid():
        data = form.cleaned_data
        part = services.add_part(job, data['name'], data['price'], data['receipt'], request.user)
        if part.flagged:
            messages.warning(request, 'Part sent to the homeowner. The new total is above the predicted range, '
                                      'so payout for this part is held until FixItFast checks the receipt.')
        else:
            messages.success(request, 'Part sent to the homeowner for approval.')
    else:
        messages.error(request, 'Please give the part name, price and a receipt photo.')
    return redirect('partner:job_detail', pk=pk)
