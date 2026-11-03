"""Homeowner storefront: user stories H01 to H11."""
from datetime import datetime

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login
from django.db.models import ProtectedError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import homeowner_required
from accounts.forms import AddressForm, ProfileForm, ReasonForm, RegisterForm
from catalogue.models import Category
from catalogue.tiers import TIER_INFO, tier_cards
from handymen.services import average_rating, completed_jobs, slot_calendar, timing_band_for
from jobs import services
from jobs.models import Job, JobPhoto, PartLine
from pricing.predictor import predict_ranges

from .forms import ClaimForm, JobRequestForm, ProblemForm, RatingForm

Status = Job.Status


def home(request):
    categories = Category.objects.filter(is_active=True).prefetch_related('job_types')
    return render(request, 'storefront/home.html', {'categories': categories})


# ---------------------------------------------------------------------------
# H01: register and manage account
# ---------------------------------------------------------------------------

def register(request):
    form = RegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        messages.success(request, 'Welcome to FixItFast! Add an address to start booking.')
        return redirect('storefront:account')
    return render(request, 'storefront/register.html', {'form': form})


@homeowner_required
def account(request):
    user = request.user
    profile_form = ProfileForm(request.POST if 'save_profile' in request.POST else None, instance=user)
    address_form = AddressForm(request.POST if 'add_address' in request.POST else None)

    if request.method == 'POST':
        if 'save_profile' in request.POST and profile_form.is_valid():
            profile_form.save()
            messages.success(request, 'Profile updated.')
            return redirect('storefront:account')
        if 'add_address' in request.POST and address_form.is_valid():
            address = address_form.save(commit=False)
            address.homeowner = user
            address.is_default = not user.addresses.exists()
            address.save()
            messages.success(request, f'Address saved ({address.get_planning_region_display()} region).')
            return redirect(request.GET.get('next') or 'storefront:account')

    return render(request, 'storefront/account.html', {
        'profile_form': profile_form,
        'address_form': address_form,
        'addresses': user.addresses.order_by('-is_default', 'label'),
    })


@require_POST
@homeowner_required
def address_default(request, pk):
    address = get_object_or_404(request.user.addresses, pk=pk)
    request.user.addresses.update(is_default=False)
    address.is_default = True
    address.save()
    return redirect('storefront:account')


@require_POST
@homeowner_required
def address_delete(request, pk):
    address = get_object_or_404(request.user.addresses, pk=pk)
    try:
        address.delete()
        messages.success(request, 'Address removed.')
    except ProtectedError:
        messages.error(request, 'This address is used by a job and cannot be removed.')
    return redirect('storefront:account')


# ---------------------------------------------------------------------------
# H02: browse services and compare tiers (no login needed)
# ---------------------------------------------------------------------------

def services_list(request):
    categories = Category.objects.filter(is_active=True).prefetch_related('job_types')
    return render(request, 'storefront/services.html', {'categories': categories})


def tiers(request):
    return render(request, 'storefront/tiers.html', {'tiers': tier_cards()})


# ---------------------------------------------------------------------------
# H03 to H06: booking flow
# ---------------------------------------------------------------------------

def next_step(job):
    """Where to send the homeowner to continue a job."""
    if job.status == Status.DRAFT:
        return redirect('storefront:choose_slot' if job.tier else 'storefront:choose_tier', pk=job.pk)
    if job.status == Status.REQUESTED:
        return redirect('storefront:recommendation', pk=job.pk)
    if job.status == Status.AWAITING_PAYMENT:
        return redirect('storefront:quotation', pk=job.pk)
    return redirect('storefront:job_detail', pk=job.pk)


def booking_job(request, pk, *allowed_statuses):
    """Fetch the homeowner's job for a booking step, or None if the step does not apply."""
    job = get_object_or_404(Job, pk=pk, homeowner=request.user)
    if request.user.is_deactivated:
        messages.error(request, 'Your account has been deactivated, so new bookings are not possible.')
        return job, redirect('storefront:job_detail', pk=job.pk)
    if job.status not in allowed_statuses:
        return job, next_step(job)
    return job, None


@homeowner_required
def job_request(request):
    """H03: create a draft job with the description, photos and address."""
    if request.user.is_deactivated:
        messages.error(request, 'Your account has been deactivated, so new bookings are not possible.')
        return redirect('storefront:jobs')
    if not request.user.addresses.exists():
        messages.info(request, 'Please add an address first.')
        return redirect('storefront:account')

    initial = {'job_type': request.GET.get('job_type')}
    form = JobRequestForm(request.POST or None, request.FILES or None, homeowner=request.user, initial=initial)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data
        job = Job.objects.create(homeowner=request.user, job_type=data['job_type'],
                                 address=data['address'], description=data['description'])
        for image in data['photos']:
            JobPhoto.objects.create(job=job, image=image, kind=JobPhoto.Kind.REQUEST)
        services.set_status(job, Status.DRAFT, request.user, 'Request created')
        return redirect('storefront:choose_tier', pk=job.pk)
    return render(request, 'storefront/job_request.html', {'form': form})


@homeowner_required
def choose_tier(request, pk):
    """H04: only tiers at or above the job type's minimum are shown, with no price."""
    job, redirect_response = booking_job(request, pk, Status.DRAFT)
    if redirect_response:
        return redirect_response
    allowed = job.job_type.allowed_tiers()
    if request.method == 'POST':
        tier = request.POST.get('tier')
        if tier in allowed:
            job.tier = tier
            job.save()
            return redirect('storefront:choose_slot', pk=job.pk)
        messages.error(request, 'Please choose one of the tiers shown.')
    return render(request, 'storefront/choose_tier.html', {'job': job, 'tiers': tier_cards(allowed)})


@homeowner_required
def choose_slot(request, pk):
    """H05 [ML]: slot calendar with a predicted price range beside each slot."""
    job, redirect_response = booking_job(request, pk, Status.DRAFT)
    if redirect_response:
        return redirect_response
    if not job.tier:
        return redirect('storefront:choose_tier', pk=job.pk)

    # Only the timing band differs between slots, so predict once per band and reuse.
    ranges, model_version = predict_ranges(job.job_type.name, job.tier, job.address.planning_region)
    calendar = slot_calendar(job.job_type, job.tier)

    if request.method == 'POST':
        try:
            slot_start = datetime.fromisoformat(request.POST['slot'])
        except (KeyError, ValueError):
            slot_start = None
        if slot_start not in calendar:
            messages.error(request, 'That slot is no longer available. Please choose another.')
            return redirect('storefront:choose_slot', pk=job.pk)
        services.choose_slot(job, slot_start, ranges, model_version, request.user)
        return redirect('storefront:recommendation', pk=job.pk)

    # Group slots by day for display: [(date, [(start, band, range, count)])]
    days = {}
    for start in calendar:
        band = timing_band_for(start)
        local = timezone.localtime(start)
        days.setdefault(local.date(), []).append({
            'start': start, 'value': start.isoformat(), 'band': band, 'range': ranges[band],
            'handymen': len(calendar[start]),
        })
    return render(request, 'storefront/choose_slot.html', {
        'job': job, 'days': days.items(), 'ranges': ranges,
    })


@homeowner_required
def recommendation(request, pk):
    """H06: one recommended handyman; declining shows the next-highest score."""
    job, redirect_response = booking_job(request, pk, Status.REQUESTED)
    if redirect_response:
        return redirect_response

    rec = services.current_recommendation(job)
    if rec is None:
        services.reset_to_slot_choice(job, request.user)
        messages.warning(request, 'No other handyman is free for that slot. Please choose another slot or tier.')
        return redirect('storefront:choose_slot', pk=job.pk)

    if request.method == 'POST':
        if request.POST.get('action') == 'accept':
            services.accept_recommendation(job, rec, request.user)
            messages.success(request, f'{rec.handyman.user.get_full_name()} has been asked to quote for your job.')
            return redirect('storefront:job_detail', pk=job.pk)
        services.decline_recommendation(rec)
        return redirect('storefront:recommendation', pk=job.pk)

    handyman = rec.handyman
    return render(request, 'storefront/recommendation.html', {
        'job': job,
        'rec': rec,
        'handyman': handyman,
        'rating': average_rating(handyman),
        'completed': completed_jobs(handyman).count(),
        'certificates': handyman.documents.filter(status='APPROVED', doc_type__in=['CERTIFICATE', 'LICENCE']),
        'reviews': handyman.reviews.select_related('job__job_type')[:3],
    })


# ---------------------------------------------------------------------------
# H07: accept the quotation and pay
# ---------------------------------------------------------------------------

@homeowner_required
def quotation(request, pk):
    job = get_object_or_404(Job, pk=pk, homeowner=request.user)
    quote = job.released_quotation
    if job.status != Status.AWAITING_PAYMENT or quote is None:
        return next_step(job)

    fee, waived = services.service_fee_for(job)
    if request.method == 'POST':
        if request.POST.get('action') == 'pay':
            services.pay_quotation(job, quote, request.user)
            messages.success(request, 'Payment received. Your appointment is confirmed.')
            return redirect('storefront:job_detail', pk=job.pk)
        services.reject_quotation(job, quote, request.user)
        messages.info(request, 'Quotation rejected. Here is the next recommended handyman.')
        return redirect('storefront:recommendation', pk=job.pk)

    return render(request, 'storefront/quotation.html', {
        'job': job, 'quote': quote, 'fee': fee, 'waived': waived,
        'full_fee': settings.SERVICE_FEE, 'total': quote.amount + fee,
    })


# ---------------------------------------------------------------------------
# H08: track, cancel and review past jobs
# ---------------------------------------------------------------------------

@homeowner_required
def jobs(request):
    all_jobs = request.user.jobs.select_related('job_type', 'handyman__user')
    active = [j for j in all_jobs if j.status not in Job.CLOSED_STATUSES or (
        j.status == Status.COMPLETED and not j.confirmed_at)]
    past = [j for j in all_jobs if j not in active]
    return render(request, 'storefront/jobs.html', {'active': active, 'past': past})


@homeowner_required
def job_detail(request, pk):
    job = get_object_or_404(Job.objects.select_related('job_type', 'handyman__user', 'address'),
                            pk=pk, homeowner=request.user)
    return render(request, 'storefront/job_detail.html', {
        'job': job,
        'events': job.events.all(),
        'photos': job.photos.all(),
        'parts': job.parts.all(),
        'quote': job.accepted_quotation or job.released_quotation,
        'claims': job.claims.all(),
        'summary': services.payment_summary(job),
        'can_claim': services.can_claim(job),
        'pending_parts': job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.PENDING),
        'continue_booking': job.status in (Status.DRAFT, Status.REQUESTED, Status.AWAITING_PAYMENT),
    })


@homeowner_required
def cancel_job(request, pk):
    job = get_object_or_404(Job, pk=pk, homeowner=request.user)
    if not job.can_cancel and job.status != Status.DRAFT:
        messages.error(request, 'This job can no longer be cancelled.')
        return redirect('storefront:job_detail', pk=job.pk)
    form = ReasonForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        services.cancel_job(job, request.user, form.cleaned_data['reason'])
        messages.success(request, 'Job cancelled. Any payment has been refunded in full.')
        return redirect('storefront:job_detail', pk=job.pk)
    return render(request, 'storefront/cancel_job.html', {'job': job, 'form': form, 'paid': job.is_paid})


@homeowner_required
def receipt(request, pk):
    job = get_object_or_404(Job, pk=pk, homeowner=request.user)
    return render(request, 'storefront/receipt.html', {
        'job': job, 'quote': job.accepted_quotation, 'ledger': job.ledger.all(),
        'summary': services.payment_summary(job),
        'parts': job.parts.filter(homeowner_decision=PartLine.HomeownerDecision.APPROVED),
    })


# ---------------------------------------------------------------------------
# H09: approve an added part or decline the repair
# ---------------------------------------------------------------------------

@homeowner_required
def part_decision(request, pk):
    part = get_object_or_404(PartLine, pk=pk, job__homeowner=request.user)
    job = part.job
    if part.homeowner_decision != PartLine.HomeownerDecision.PENDING or job.status != Status.IN_PROGRESS:
        return redirect('storefront:job_detail', pk=job.pk)

    if request.method == 'POST':
        if request.POST.get('action') == 'approve':
            services.approve_part(part, request.user)
            messages.success(request, f'{part.name} approved and paid.')
        else:
            services.decline_repair(job, part, request.user)
            messages.info(request, 'Repair declined. You have been refunded, less the consultation fee.')
        return redirect('storefront:job_detail', pk=job.pk)

    current = services.job_total(job)
    return render(request, 'storefront/part_decision.html', {
        'job': job, 'part': part, 'current_total': current, 'revised_total': current + part.price,
        'consultation_fee': settings.CONSULTATION_FEE,
    })


# ---------------------------------------------------------------------------
# H10: confirm completion and rate
# ---------------------------------------------------------------------------

@homeowner_required
def confirm_job(request, pk):
    job = get_object_or_404(Job, pk=pk, homeowner=request.user, status=Status.COMPLETED)
    has_review = hasattr(job, 'review')
    if job.confirmed_at and has_review:
        return redirect('storefront:job_detail', pk=job.pk)

    rating_form = RatingForm(request.POST if 'rate' in request.POST else None)
    problem_form = ProblemForm(request.POST if 'problem' in request.POST else None)

    if request.method == 'POST':
        if 'rate' in request.POST and rating_form.is_valid():
            if not job.confirmed_at:
                services.confirm_completion(job, request.user)
            if not has_review:
                services.submit_review(job, rating_form.cleaned_data['rating'], rating_form.cleaned_data['comment'])
            messages.success(request, 'Thanks! Payment has been released and your warranty has started.')
            return redirect('storefront:job_detail', pk=job.pk)
        if 'problem' in request.POST and problem_form.is_valid() and not job.confirmed_at:
            services.report_problem(job, problem_form.cleaned_data['note'], request.user)
            messages.info(request, 'Thanks for telling us. A FixItFast employee will contact you.')
            return redirect('storefront:job_detail', pk=job.pk)

    return render(request, 'storefront/confirm_job.html', {
        'job': job, 'rating_form': rating_form, 'problem_form': problem_form,
        'photos': job.photos.exclude(kind=JobPhoto.Kind.REQUEST),
    })


# ---------------------------------------------------------------------------
# H11: raise a warranty claim
# ---------------------------------------------------------------------------

@homeowner_required
def raise_claim(request, pk):
    job = get_object_or_404(Job, pk=pk, homeowner=request.user, status=Status.COMPLETED)
    if not services.can_claim(job):
        messages.error(request, 'This job is outside its warranty period or already has an open claim.')
        return redirect('storefront:job_detail', pk=job.pk)
    form = ClaimForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        services.raise_claim(job, form.cleaned_data['description'], form.cleaned_data['photos'])
        messages.success(request, 'Claim submitted. We will update you here.')
        return redirect('storefront:job_detail', pk=job.pk)
    return render(request, 'storefront/claim.html', {'job': job, 'form': form, 'tier_info': TIER_INFO[job.tier]})
