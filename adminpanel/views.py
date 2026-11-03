"""Internal admin panel: user stories A02 to A10. A01 (login and roles) is in accounts."""
from datetime import timedelta

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.decorators import staff_required
from accounts.forms import ReasonForm
from accounts.models import User
from accounts.services import notify
from catalogue.models import Category, JobType, Tier
from handymen.models import Document, HandymanProfile, TierChange
from handymen.services import (
    average_rating, completed_jobs, eligible_for_upgrade, meets_tier, tier_requirements,
)
from jobs import services
from jobs.models import Job, LedgerEntry, PartLine, Quotation, WarrantyClaim

from .forms import CategoryForm, DowngradeForm, JobFilterForm, JobTypeForm, RefundForm, ReturnVisitForm

Status = Job.Status
Entry = LedgerEntry.EntryType


def share(numerator, denominator):
    """Percentage for the launch measures, or None when there is no data yet."""
    return round(100 * numerator / denominator) if denominator else None


# ---------------------------------------------------------------------------
# A02: operations dashboard
# ---------------------------------------------------------------------------

def launch_measures():
    """The three launch measures from Section 1.7."""
    # 1. Share of requests matched to a handyman within 24 hours.
    requested = Job.objects.filter(requested_at__isnull=False)
    matched = sum(1 for j in requested if j.matched_at and j.matched_at - j.requested_at <= timedelta(hours=24))

    # 2. Share of quotations released automatically by the model (the ML decision).
    quotations = Quotation.objects.count()
    auto = Quotation.objects.filter(auto_released=True).count()

    # 3. Share of homeowners who book a second job within six months of their first.
    paid_jobs = Job.objects.filter(ledger__entry_type=Entry.QUOTATION_HELD).distinct()
    first_dates = {}
    repeaters = set()
    for job in paid_jobs.order_by('homeowner_id', 'slot_start'):
        first = first_dates.setdefault(job.homeowner_id, job.slot_start)
        if job.slot_start != first and job.slot_start - first <= timedelta(days=183):
            repeaters.add(job.homeowner_id)

    return [
        ('Requests matched within 24 hours', share(matched, requested.count()), f'{matched} of {requested.count()}'),
        ('Quotations released automatically', share(auto, quotations), f'{auto} of {quotations}'),
        ('Homeowners booking again within 6 months', share(len(repeaters), len(first_dates)),
         f'{len(repeaters)} of {len(first_dates)}'),
    ]


def monthly_revenue():
    """Manager-only: this month's platform revenue by source (Section 1.3)."""
    month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    entries = LedgerEntry.objects.filter(created_at__gte=month_start)
    total = lambda qs: qs.aggregate(t=Sum('amount'))['t'] or 0
    commission = total(entries.filter(entry_type=Entry.COMMISSION).exclude(job__status=Status.DECLINED))
    consultation = total(entries.filter(entry_type=Entry.COMMISSION, job__status=Status.DECLINED))
    service_fees = total(entries.filter(entry_type=Entry.SERVICE_FEE).exclude(
        job__status__in=[Status.CANCELLED, Status.DECLINED]))
    return {'commission': commission, 'service_fees': service_fees, 'consultation': consultation,
            'total': commission + service_fees + consultation}


@staff_required
def dashboard(request):
    today = timezone.localdate()
    queues = [
        ('Handyman applications', HandymanProfile.objects.filter(
            application_status=HandymanProfile.ApplicationStatus.PENDING).count()
         + Document.objects.filter(status=Document.Status.PENDING,
                                   handyman__application_status=HandymanProfile.ApplicationStatus.APPROVED).count(),
         'adminpanel:applications', 'bi-person-check'),
        ('Quotation reviews', Quotation.objects.filter(status=Quotation.Status.UNDER_REVIEW).count(),
         'adminpanel:quotation_queue', 'bi-cash-coin'),
        ('Flagged parts', flagged_parts().count(), 'adminpanel:part_queue', 'bi-receipt'),
        ('Warranty claims', WarrantyClaim.objects.filter(
            status__in=[WarrantyClaim.Status.SUBMITTED, WarrantyClaim.Status.UNDER_REVIEW]).count(),
         'adminpanel:claims', 'bi-shield-check'),
    ]
    todays_jobs = Job.objects.filter(slot_start__date=today).select_related('job_type', 'handyman__user')
    by_status = todays_jobs.values('status').annotate(n=Count('id')).order_by()
    status_labels = dict(Status.choices)
    return render(request, 'adminpanel/dashboard.html', {
        'queues': queues,
        'todays_jobs': todays_jobs.order_by('slot_start'),
        'today_by_status': [(status_labels[row['status']], row['status'], row['n']) for row in by_status],
        'measures': launch_measures(),
        'revenue': monthly_revenue() if request.user.is_manager else None,
        'problems': Job.objects.exclude(problem_note='').filter(confirmed_at__isnull=True, status=Status.COMPLETED),
    })


# ---------------------------------------------------------------------------
# A03: service catalogue and minimum tiers
# ---------------------------------------------------------------------------

@staff_required
def catalogue(request):
    return render(request, 'adminpanel/catalogue.html', {
        'categories': Category.objects.annotate(n=Count('job_types')),
        'job_types': JobType.objects.select_related('category').annotate(n_jobs=Count('jobs')),
    })


@staff_required
def category_form(request, pk=None):
    category = get_object_or_404(Category, pk=pk) if pk else None
    form = CategoryForm(request.POST or None, instance=category)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Category saved.')
        return redirect('adminpanel:catalogue')
    return render(request, 'adminpanel/form_page.html', {
        'form': form, 'title': f'Edit {category}' if category else 'New category', 'back': 'adminpanel:catalogue'})


@staff_required
def job_type_form(request, pk=None):
    job_type = get_object_or_404(JobType, pk=pk) if pk else None
    form = JobTypeForm(request.POST or None, instance=job_type)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Job type saved. Deactivated job types stay on past jobs.')
        return redirect('adminpanel:catalogue')
    return render(request, 'adminpanel/form_page.html', {
        'form': form, 'title': f'Edit {job_type}' if job_type else 'New job type', 'back': 'adminpanel:catalogue',
        'help': 'Simple jobs can be open to all tiers; complex jobs should be Platinum only. '
                'New job types stay inactive until the pricing model has been retrained to include them.'})


# ---------------------------------------------------------------------------
# A04: verify handyman applications
# ---------------------------------------------------------------------------

@staff_required
def applications(request):
    return render(request, 'adminpanel/applications.html', {
        'pending': HandymanProfile.objects.filter(
            application_status=HandymanProfile.ApplicationStatus.PENDING).select_related('user')
            .annotate(docs=Count('documents')).order_by('created_at'),
        'new_documents': Document.objects.filter(
            status=Document.Status.PENDING,
            handyman__application_status=HandymanProfile.ApplicationStatus.APPROVED,
        ).select_related('handyman__user').order_by('uploaded_at'),
    })


@staff_required
def application_detail(request, pk):
    handyman = get_object_or_404(HandymanProfile.objects.select_related('user'), pk=pk)
    return render(request, 'adminpanel/application_detail.html', {
        'handyman': handyman,
        'documents': handyman.documents.prefetch_related('job_types'),
        'reason_form': ReasonForm(),
        'has_identity': handyman.documents.filter(doc_type=Document.DocType.IDENTITY,
                                                  status=Document.Status.APPROVED).exists(),
    })


@require_POST
@staff_required
def review_document(request, pk):
    doc = get_object_or_404(Document, pk=pk)
    approve = request.POST.get('action') == 'approve'
    reason = request.POST.get('reason', '').strip()
    if not approve and not reason:
        messages.error(request, 'A rejection needs a reason, which the applicant will see.')
        return redirect('adminpanel:application_detail', pk=doc.handyman_id)
    doc.status = Document.Status.APPROVED if approve else Document.Status.REJECTED
    doc.rejection_reason = '' if approve else reason
    doc.reviewed_by = request.user
    doc.reviewed_at = timezone.now()
    doc.save()
    notify(doc.handyman.user, f'Your document "{doc.title}" was {doc.get_status_display().lower()}.'
           + (f' Reason: {reason}' if reason and not approve else ''))
    messages.success(request, f'Document {doc.get_status_display().lower()}.')
    return redirect('adminpanel:application_detail', pk=doc.handyman_id)


@require_POST
@staff_required
def decide_application(request, pk):
    handyman = get_object_or_404(HandymanProfile, pk=pk)
    # Only pending applications can be decided; otherwise approving again would
    # reset an existing Gold or Platinum handyman to Standard. Tiers change in A05.
    if handyman.application_status != HandymanProfile.ApplicationStatus.PENDING:
        messages.error(request, 'This application has already been decided.')
        return redirect('adminpanel:application_detail', pk=pk)
    if request.POST.get('action') == 'approve':
        if not handyman.documents.filter(doc_type=Document.DocType.IDENTITY, status=Document.Status.APPROVED).exists():
            messages.error(request, 'Approve the identity document first.')
            return redirect('adminpanel:application_detail', pk=pk)
        handyman.application_status = HandymanProfile.ApplicationStatus.APPROVED
        handyman.approved_at = timezone.now()
        handyman.approved_by = request.user
        handyman.tier = Tier.STANDARD          # an approved handyman starts at Standard (A04)
        handyman.rejection_reason = ''
        handyman.save()
        notify(handyman.user, 'Welcome to FixItFast! Your application has been approved. Set your availability to start receiving jobs.')
        messages.success(request, f'{handyman.user.get_full_name()} approved at Standard tier.')
        return redirect('adminpanel:applications')
    form = ReasonForm(request.POST)
    if form.is_valid():
        handyman.application_status = HandymanProfile.ApplicationStatus.REJECTED
        handyman.rejection_reason = form.cleaned_data['reason']
        handyman.save()
        notify(handyman.user, f'Your application was not approved: {handyman.rejection_reason}')
        messages.success(request, 'Application rejected.')
        return redirect('adminpanel:applications')
    messages.error(request, 'A rejection needs a reason.')
    return redirect('adminpanel:application_detail', pk=pk)


# ---------------------------------------------------------------------------
# A05: handyman records and tiers
# ---------------------------------------------------------------------------

@staff_required
def handymen(request):
    q = request.GET.get('q', '').strip()
    tier = request.GET.get('tier', '')
    rows = HandymanProfile.objects.filter(
        application_status=HandymanProfile.ApplicationStatus.APPROVED).select_related('user')
    if q:
        rows = rows.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q)
                           | Q(user__email__icontains=q) | Q(user__mobile__icontains=q))
    if tier:
        rows = rows.filter(tier=tier)
    records = []
    for h in rows:
        requirements = tier_requirements(h)
        records.append({
            'handyman': h,
            'rating': average_rating(h),
            'completed': completed_jobs(h).count(),
            # The platform marks handymen who meet the next tier, or no longer meet their own.
            'upgrade': eligible_for_upgrade(h, requirements),
            'below_tier': not meets_tier(h, h.tier, requirements),
        })
    return render(request, 'adminpanel/handymen.html', {'records': records, 'q': q, 'tier': tier,
                                                        'tiers': Tier.choices})


@staff_required
def handyman_detail(request, pk):
    handyman = get_object_or_404(HandymanProfile.objects.select_related('user'), pk=pk)
    requirements = tier_requirements(handyman)
    upgrade = eligible_for_upgrade(handyman, requirements)
    return render(request, 'adminpanel/handyman_detail.html', {
        'handyman': handyman,
        'requirements': [(Tier(t).label, items) for t, items in requirements.items()],
        'upgrade_label': Tier(upgrade).label if upgrade else '',
        'below_tier': not meets_tier(handyman, handyman.tier, requirements),
        'rating': average_rating(handyman),
        'completed': completed_jobs(handyman).count(),
        'documents': handyman.documents.all(),
        'reviews': handyman.reviews.select_related('job__job_type')[:10],
        'tier_changes': handyman.tier_changes.select_related('changed_by'),
        'jobs': handyman.jobs.select_related('job_type')[:10],
        'downgrade_form': DowngradeForm(current_tier=handyman.tier) if handyman.tier != Tier.STANDARD else None,
        'reason_form': ReasonForm(),
    })


def change_tier(handyman, to_tier, reason, employee):
    TierChange.objects.create(handyman=handyman, from_tier=handyman.tier, to_tier=to_tier,
                              reason=reason, changed_by=employee)
    handyman.tier = to_tier
    handyman.save()
    # Job types above the new tier are removed so the handyman only gets jobs they qualify for.
    handyman.job_types.remove(*handyman.job_types.exclude(min_tier__in=Tier.at_or_below(to_tier)))
    notify(handyman.user, f'Your tier is now {Tier(to_tier).label}. {reason}')


@require_POST
@staff_required
def handyman_action(request, pk):
    handyman = get_object_or_404(HandymanProfile, pk=pk)
    action = request.POST.get('action')
    if action == 'upgrade':
        new_tier = eligible_for_upgrade(handyman)
        if new_tier:
            change_tier(handyman, new_tier, 'Met all requirements for the tier.', request.user)
            messages.success(request, f'Upgraded to {Tier(new_tier).label}.')
        else:
            messages.error(request, 'The handyman does not meet the next tier yet.')
    elif action == 'downgrade':
        form = DowngradeForm(request.POST, current_tier=handyman.tier)
        if form.is_valid():
            change_tier(handyman, form.cleaned_data['to_tier'], form.cleaned_data['reason'], request.user)
            messages.success(request, 'Tier changed.')
        else:
            messages.error(request, 'Choose a lower tier and give a reason.')
    elif action in ('suspend', 'unsuspend'):
        form = ReasonForm(request.POST)
        if action == 'unsuspend':
            handyman.is_suspended = False
            handyman.suspension_reason = ''
            handyman.save()
            notify(handyman.user, 'Your account is active again.')
            messages.success(request, 'Suspension lifted.')
        elif form.is_valid():
            handyman.is_suspended = True
            handyman.suspension_reason = form.cleaned_data['reason']
            handyman.save()
            notify(handyman.user, f'Your account has been suspended: {handyman.suspension_reason}')
            messages.success(request, 'Handyman suspended. They will receive no new jobs.')
        else:
            messages.error(request, 'A suspension needs a reason.')
    return redirect('adminpanel:handyman_detail', pk=pk)


# ---------------------------------------------------------------------------
# A06 [ML]: out-of-range quotation review
# ---------------------------------------------------------------------------

@staff_required
def quotation_queue(request):
    # Nearest appointments first. Quotations inside the range never come here.
    queue = Quotation.objects.filter(status=Quotation.Status.UNDER_REVIEW).select_related(
        'job__job_type', 'handyman__user').order_by('job__slot_start')
    return render(request, 'adminpanel/quotation_queue.html', {'queue': queue})


@staff_required
def quotation_detail(request, pk):
    quote = get_object_or_404(Quotation.objects.select_related('job__job_type', 'handyman__user'), pk=pk)
    job = quote.job
    form = ReasonForm(request.POST if request.POST.get('action') == 'return' else None)
    awaiting_review = (quote.status == Quotation.Status.UNDER_REVIEW
                       and job.status == Status.QUOTATION_UNDER_REVIEW)
    if request.method == 'POST' and awaiting_review:
        if request.POST.get('action') == 'release':
            services.release_quotation(quote, request.user, request.POST.get('note', ''))
            messages.success(request, 'Quotation released to the homeowner.')
            return redirect('adminpanel:quotation_queue')
        if form.is_valid():
            services.return_quotation(quote, request.user, form.cleaned_data['reason'])
            messages.success(request, 'Quotation returned to the handyman.')
            return redirect('adminpanel:quotation_queue')

    # How far the quotation is from the range, to help the employee decide.
    if quote.amount > job.price_p90:
        distance = f'S${quote.amount - job.price_p90:.2f} above the top of the range'
    elif quote.amount < job.price_p10:
        distance = f'S${job.price_p10 - quote.amount:.2f} below the bottom of the range'
    else:
        distance = 'inside the range'
    return render(request, 'adminpanel/quotation_detail.html', {
        'quote': quote, 'job': job, 'form': form, 'distance': distance,
        'photos': job.photos.all(),
        'deviation': round(100 * (quote.amount - job.price_p50) / job.price_p50),
    })


# ---------------------------------------------------------------------------
# A07 [ML]: flagged part lines
# ---------------------------------------------------------------------------

def flagged_parts():
    return PartLine.objects.filter(flagged=True, review_status=PartLine.ReviewStatus.PENDING,
                                   homeowner_decision=PartLine.HomeownerDecision.APPROVED)


@staff_required
def part_queue(request):
    return render(request, 'adminpanel/part_queue.html', {
        'parts': flagged_parts().select_related('job__job_type', 'job__handyman__user').order_by('created_at')})


@staff_required
def part_detail(request, pk):
    part = get_object_or_404(PartLine.objects.select_related('job__job_type'), pk=pk)
    job = part.job
    form = ReasonForm(request.POST if request.POST.get('action') == 'reject' else None)
    if request.method == 'POST' and part.review_status == PartLine.ReviewStatus.PENDING:
        if request.POST.get('action') == 'approve':
            services.review_part(part, request.user, approve=True)
            messages.success(request, 'Part approved; payout released with the job.')
            return redirect('adminpanel:part_queue')
        if form.is_valid():
            services.review_part(part, request.user, approve=False, reason=form.cleaned_data['reason'])
            messages.success(request, 'Part rejected and refunded to the homeowner.')
            return redirect('adminpanel:part_queue')
    return render(request, 'adminpanel/part_detail.html', {
        'part': part, 'job': job, 'form': form,
        'quote': job.accepted_quotation, 'job_total': services.job_total(job),
    })


# ---------------------------------------------------------------------------
# A08: warranty claims
# ---------------------------------------------------------------------------

@staff_required
def claims(request):
    status = request.GET.get('status', 'open')
    rows = WarrantyClaim.objects.select_related('job__job_type', 'job__homeowner')
    if status == 'open':
        rows = rows.exclude(status__in=[WarrantyClaim.Status.RESOLVED, WarrantyClaim.Status.REJECTED])
    return render(request, 'adminpanel/claims.html', {'claims': rows, 'status': status})


@staff_required
def claim_detail(request, pk):
    claim = get_object_or_404(WarrantyClaim.objects.select_related('job__job_type', 'job__handyman__user'), pk=pk)
    job = claim.job
    if claim.status == WarrantyClaim.Status.SUBMITTED:
        claim.status = WarrantyClaim.Status.UNDER_REVIEW
        claim.handled_by = request.user
        claim.save()

    action = request.POST.get('action')
    summary = services.payment_summary(job)
    refund_form = RefundForm(request.POST if action == 'refund' else None, max_amount=summary['net_paid'])
    visit_form = ReturnVisitForm(request.POST if action == 'visit' else None, job=job)
    reject_form = ReasonForm(request.POST if action == 'reject' else None)
    closed = claim.status in (WarrantyClaim.Status.RESOLVED, WarrantyClaim.Status.REJECTED)

    if request.method == 'POST' and not closed:
        if action == 'refund' and job.tier == Tier.STANDARD and refund_form.is_valid():
            services.refund_claim(claim, request.user, refund_form.cleaned_data['amount'], refund_form.cleaned_data['note'])
            messages.success(request, 'Refund issued and claim resolved.')
            return redirect('adminpanel:claims')
        if action == 'visit' and job.tier != Tier.STANDARD and visit_form.is_valid():
            data = visit_form.cleaned_data
            services.schedule_return_visit(claim, request.user, data['when'], data.get('replacement'), data['note'])
            messages.success(request, 'Return visit scheduled.')
            return redirect('adminpanel:claim_detail', pk=pk)
        if action == 'resolve':
            services.update_claim(claim, request.user, WarrantyClaim.Status.RESOLVED, request.POST.get('note', ''))
            messages.success(request, 'Claim resolved.')
            return redirect('adminpanel:claims')
        if action == 'reject' and reject_form.is_valid():
            services.update_claim(claim, request.user, WarrantyClaim.Status.REJECTED, reject_form.cleaned_data['reason'])
            messages.success(request, 'Claim rejected.')
            return redirect('adminpanel:claims')

    return render(request, 'adminpanel/claim_detail.html', {
        'claim': claim, 'job': job, 'closed': closed, 'summary': summary,
        'refund_form': refund_form, 'visit_form': visit_form, 'reject_form': reject_form,
        'original_available': services.original_available_soon(job) if job.tier == Tier.PLATINUM else None,
        'photos': claim.photos.all(),
        'work_photos': job.photos.exclude(kind='REQUEST'),
    })


# ---------------------------------------------------------------------------
# A09: homeowner records
# ---------------------------------------------------------------------------

@staff_required
def homeowners(request):
    q = request.GET.get('q', '').strip()
    rows = User.objects.filter(role=User.Role.HOMEOWNER).annotate(n_jobs=Count('jobs'))
    if q:
        rows = rows.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q)
                           | Q(email__icontains=q) | Q(mobile__icontains=q))
    return render(request, 'adminpanel/homeowners.html', {'homeowners': rows.order_by('first_name'), 'q': q})


@staff_required
def homeowner_detail(request, pk):
    homeowner = get_object_or_404(User, pk=pk, role=User.Role.HOMEOWNER)
    form = ReasonForm(request.POST if request.POST.get('action') == 'deactivate' else None)
    if request.method == 'POST':
        if request.POST.get('action') == 'reactivate':
            homeowner.is_deactivated = False
            homeowner.deactivation_reason = ''
            homeowner.save()
            messages.success(request, 'Account reactivated.')
            return redirect('adminpanel:homeowner_detail', pk=pk)
        if form.is_valid():
            homeowner.is_deactivated = True
            homeowner.deactivation_reason = form.cleaned_data['reason']
            homeowner.save()
            messages.success(request, 'Account deactivated. Records are kept.')
            return redirect('adminpanel:homeowner_detail', pk=pk)
    jobs = homeowner.jobs.select_related('job_type')
    return render(request, 'adminpanel/homeowner_detail.html', {
        'homeowner': homeowner, 'form': form, 'addresses': homeowner.addresses.all(), 'jobs': jobs,
        'claims': WarrantyClaim.objects.filter(job__homeowner=homeowner).select_related('job__job_type'),
        'total_paid': sum(services.payment_summary(j)['net_paid'] for j in jobs),
    })


# ---------------------------------------------------------------------------
# A10: jobs and payment records
# ---------------------------------------------------------------------------

@staff_required
def jobs(request):
    form = JobFilterForm(request.GET or None)
    rows = Job.objects.exclude(status=Status.DRAFT).select_related('job_type__category', 'homeowner', 'handyman__user')
    if form.is_valid():
        data = form.cleaned_data
        if data['status']:
            rows = rows.filter(status=data['status'])
        if data['tier']:
            rows = rows.filter(tier=data['tier'])
        if data['category']:
            rows = rows.filter(job_type__category=data['category'])
        if data['timing_band']:
            rows = rows.filter(timing_band=data['timing_band'])
        if data['date']:
            rows = rows.filter(slot_start__date=data['date'])
    return render(request, 'adminpanel/jobs.html', {'form': form, 'jobs': rows.order_by('-slot_start')[:200]})


@staff_required
def job_detail(request, pk):
    job = get_object_or_404(Job.objects.select_related('job_type', 'homeowner', 'handyman__user', 'address'), pk=pk)
    form = ReasonForm(request.POST or None)
    if request.method == 'POST' and job.status not in Job.CLOSED_STATUSES and form.is_valid():
        # Exceptional cancellation by staff: always a full refund (A10).
        services.cancel_job(job, request.user, form.cleaned_data['reason'])
        messages.success(request, 'Job cancelled with a full refund.')
        return redirect('adminpanel:job_detail', pk=pk)
    return render(request, 'adminpanel/job_detail.html', {
        'job': job, 'form': form,
        'ledger': job.ledger.select_related('created_by', 'part'),
        'summary': services.payment_summary(job),
        'events': job.events.select_related('actor'),
        'quotations': job.quotations.select_related('handyman__user', 'reviewed_by'),
        'parts': job.parts.all(),
        'recommendations': job.recommendations.select_related('handyman__user'),
        'claims': job.claims.all(),
        'photos': job.photos.all(),
    })
