"""Admin panel workflow, A01 to A10, including the edge cases where states interact.

    python manage.py test adminpanel
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalogue.management.commands.seed_demo import DEMO_PASSWORD
from catalogue.models import JobType, Tier
from handymen.models import HandymanProfile, TierChange
from jobs.models import Job, LedgerEntry, PartLine, Quotation, WarrantyClaim
from jobs.tests import image

TEMP_MEDIA = tempfile.mkdtemp()
Entry = LedgerEntry.EntryType
HOMEOWNER = 'homeowner@fixitfast.test'
GOLD = 'gold@fixitfast.test'
EMPLOYEE = 'employee@fixitfast.test'
MANAGER = 'manager@fixitfast.test'


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class AdminWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    # -- helpers ------------------------------------------------------------

    def login(self, section, email):
        self.client.logout()
        response = self.client.post(reverse(f'{section}:login'), {'username': email, 'password': DEMO_PASSWORD})
        self.assertEqual(response.status_code, 302, f'login failed for {email}')

    def book(self, tier=Tier.GOLD, job_type='Tap replacement'):
        self.login('storefront', HOMEOWNER)
        address = User.objects.get(email=HOMEOWNER).addresses.first()
        self.client.post(reverse('storefront:job_request'), {
            'job_type': JobType.objects.get(name=job_type).pk, 'address': address.pk,
            'description': 'The kitchen tap drips constantly and the handle is loose.'})
        job = Job.objects.latest('pk')
        self.client.post(reverse('storefront:choose_tier', args=[job.pk]), {'tier': tier})
        days = list(self.client.get(reverse('storefront:choose_slot', args=[job.pk])).context['days'])
        self.client.post(reverse('storefront:choose_slot', args=[job.pk]), {'slot': days[0][1][0]['value']})
        self.client.post(reverse('storefront:recommendation', args=[job.pk]), {'action': 'accept'})
        job.refresh_from_db()
        return job

    def quote(self, job, amount, justification='Bigger job than usual.'):
        self.login('partner', job.handyman.user.email)
        self.client.post(reverse('partner:submit_quote', args=[job.pk]),
                         {'amount': amount, 'work_description': 'Replace tap.', 'justification': justification})
        job.refresh_from_db()

    def pay(self, job):
        self.login('storefront', HOMEOWNER)
        self.client.post(reverse('storefront:quotation', args=[job.pk]), {'action': 'pay'})
        job.refresh_from_db()

    def start_with_flagged_part(self, job):
        """Pay, start the visit, add a part above P90 and have the homeowner approve it."""
        self.pay(job)
        self.login('partner', job.handyman.user.email)
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        self.client.post(reverse('partner:add_part', args=[job.pk]),
                         {'name': 'Mixer tap', 'price': '200', 'receipt': image('r.png')})
        part = job.parts.get()
        self.assertTrue(part.flagged)
        self.login('storefront', HOMEOWNER)
        self.client.post(reverse('storefront:part_decision', args=[part.pk]), {'action': 'approve'})
        part.refresh_from_db()
        return part

    # -- A01 ----------------------------------------------------------------

    def test_role_access(self):
        self.assertRedirects(self.client.get(reverse('adminpanel:dashboard')),
                             reverse('adminpanel:login') + '?next=' + reverse('adminpanel:dashboard'))
        self.login('storefront', HOMEOWNER)
        self.assertEqual(self.client.get(reverse('adminpanel:jobs')).status_code, 403)
        self.login('adminpanel', EMPLOYEE)
        self.assertNotContains(self.client.get(reverse('adminpanel:dashboard')), 'Revenue this month')
        self.login('adminpanel', MANAGER)
        self.assertContains(self.client.get(reverse('adminpanel:dashboard')), 'Revenue this month')

    # -- A02 ----------------------------------------------------------------

    def test_dashboard_counts_and_measures(self):
        job = self.book()
        self.quote(job, job.price_p90 + 50)
        self.login('adminpanel', MANAGER)
        page = self.client.get(reverse('adminpanel:dashboard'))
        queues = {label: count for label, count, *_ in page.context['queues']}
        self.assertEqual(queues['Quotation reviews'], 1)
        measures = {label: pct for label, pct, _ in page.context['measures']}
        self.assertEqual(measures['Requests matched within 24 hours'], 100)
        self.assertEqual(measures['Quotations released automatically'], 0)

    # -- A03 ----------------------------------------------------------------

    def test_catalogue_minimum_tier_controls_storefront(self):
        tap = JobType.objects.get(name='Tap replacement')
        self.login('adminpanel', EMPLOYEE)
        self.client.post(reverse('adminpanel:job_type_edit', args=[tap.pk]), {
            'category': tap.category.pk, 'name': tap.name, 'min_tier': Tier.PLATINUM, 'is_active': 'on'})
        tap.refresh_from_db()
        self.assertEqual(tap.min_tier, Tier.PLATINUM)
        self.assertEqual(tap.allowed_tiers(), [Tier.PLATINUM])
        # Deactivating hides it from the booking form.
        self.client.post(reverse('adminpanel:job_type_edit', args=[tap.pk]), {
            'category': tap.category.pk, 'name': tap.name, 'min_tier': Tier.STANDARD})
        self.login('storefront', HOMEOWNER)
        form = self.client.get(reverse('storefront:job_request')).context['form']
        self.assertFalse(form.fields['job_type'].queryset.filter(pk=tap.pk).exists())

    # -- A04 ----------------------------------------------------------------

    def test_application_decision_cannot_demote_an_approved_handyman(self):
        gold = HandymanProfile.objects.get(user__email=GOLD)
        self.login('adminpanel', EMPLOYEE)
        self.client.post(reverse('adminpanel:decide_application', args=[gold.pk]), {'action': 'approve'})
        self.client.post(reverse('adminpanel:decide_application', args=[gold.pk]),
                         {'action': 'reject', 'reason': 'oops'})
        gold.refresh_from_db()
        self.assertEqual(gold.tier, Tier.GOLD)
        self.assertEqual(gold.application_status, HandymanProfile.ApplicationStatus.APPROVED)

    # -- A05 ----------------------------------------------------------------

    def test_downgrade_records_history_and_trims_job_types(self):
        gold = HandymanProfile.objects.get(user__email=GOLD)
        self.login('adminpanel', EMPLOYEE)
        self.client.post(reverse('adminpanel:handyman_action', args=[gold.pk]),
                         {'action': 'downgrade', 'to_tier': Tier.STANDARD, 'reason': 'Rating fell below 4.3'})
        gold.refresh_from_db()
        self.assertEqual(gold.tier, Tier.STANDARD)
        self.assertFalse(gold.job_types.exclude(min_tier=Tier.STANDARD).exists())
        self.assertTrue(TierChange.objects.filter(handyman=gold, to_tier=Tier.STANDARD).exists())
        # Upgrade is refused because the requirements are not met.
        self.client.post(reverse('adminpanel:handyman_action', args=[gold.pk]), {'action': 'upgrade'})
        gold.refresh_from_db()
        self.assertEqual(gold.tier, Tier.STANDARD)

    # -- A06 ----------------------------------------------------------------

    def test_return_then_resubmit_then_release(self):
        job = self.book()
        self.quote(job, job.price_p90 + 50)
        quote = job.quotations.get()
        self.login('adminpanel', EMPLOYEE)
        self.client.post(reverse('adminpanel:quotation_detail', args=[quote.pk]),
                         {'action': 'return', 'reason': 'Please itemise the hose.'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUOTATION_PENDING)
        self.quote(job, job.price_p50)                  # revised quote inside range: auto-released
        self.assertEqual(job.status, Job.Status.AWAITING_PAYMENT)
        quote.refresh_from_db()
        self.assertEqual(quote.reviewed_by.email, EMPLOYEE)

    def test_cancelled_job_leaves_review_queue_and_cannot_be_revived(self):
        job = self.book()
        self.quote(job, job.price_p90 + 50)
        quote = job.quotations.get()
        self.login('storefront', HOMEOWNER)
        self.client.post(reverse('storefront:cancel_job', args=[job.pk]), {'reason': 'Changed my mind'})
        self.login('adminpanel', EMPLOYEE)
        self.assertNotContains(self.client.get(reverse('adminpanel:quotation_queue')), f'#{job.pk} ')
        self.client.post(reverse('adminpanel:quotation_detail', args=[quote.pk]), {'action': 'release'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.CANCELLED)

    # -- A07 ----------------------------------------------------------------

    def test_reject_flagged_part_refunds_once(self):
        job = self.book()
        self.quote(job, job.price_p50)
        part = self.start_with_flagged_part(job)
        self.login('adminpanel', EMPLOYEE)
        self.client.post(reverse('adminpanel:part_detail', args=[part.pk]),
                         {'action': 'reject', 'reason': 'Receipt is for a different item.'})
        part.refresh_from_db()
        self.assertEqual(part.review_status, PartLine.ReviewStatus.REJECTED)
        self.assertEqual(job.ledger.get(entry_type=Entry.REFUND).amount, Decimal('200.00'))

    def test_declined_job_part_is_not_refunded_twice(self):
        job = self.book()
        self.quote(job, job.price_p50)
        part = self.start_with_flagged_part(job)
        # A second part, then the homeowner declines the repair: everything is refunded.
        self.login('partner', GOLD)
        self.client.post(reverse('partner:add_part', args=[job.pk]),
                         {'name': 'Hose', 'price': '20', 'receipt': image('h.png')})
        self.login('storefront', HOMEOWNER)
        self.client.post(reverse('storefront:part_decision', args=[job.parts.get(name='Hose').pk]),
                         {'action': 'decline'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.DECLINED)
        refunded = sum(e.amount for e in job.ledger.filter(entry_type=Entry.REFUND))

        self.login('adminpanel', EMPLOYEE)
        self.assertNotContains(self.client.get(reverse('adminpanel:part_queue')), 'Mixer tap')
        self.client.post(reverse('adminpanel:part_detail', args=[part.pk]), {'action': 'reject', 'reason': 'x'})
        self.assertEqual(sum(e.amount for e in job.ledger.filter(entry_type=Entry.REFUND)), refunded)

    # -- A08 ----------------------------------------------------------------

    def complete(self, job):
        self.login('partner', job.handyman.user.email)
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        for kind in ('BEFORE', 'AFTER'):
            self.client.post(reverse('partner:upload_photos', args=[job.pk]), {'kind': kind, 'images': [image()]})
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        self.login('storefront', HOMEOWNER)
        self.client.post(reverse('storefront:confirm_job', args=[job.pk]), {'rate': '1', 'rating': 5})
        self.client.post(reverse('storefront:raise_claim', args=[job.pk]),
                         {'description': 'Leaking again from the base of the tap.'})
        return job.claims.get()

    def test_standard_claim_refund_is_capped(self):
        job = self.book(tier=Tier.STANDARD)
        self.quote(job, job.price_p50)
        self.pay(job)
        claim = self.complete(job)
        self.login('adminpanel', EMPLOYEE)
        self.client.get(reverse('adminpanel:claim_detail', args=[claim.pk]))
        claim.refresh_from_db()
        self.assertEqual(claim.status, WarrantyClaim.Status.UNDER_REVIEW)
        response = self.client.post(reverse('adminpanel:claim_detail', args=[claim.pk]),
                                    {'action': 'refund', 'amount': '9999', 'note': 'x'})
        self.assertContains(response, 'Cannot refund more than')
        self.client.post(reverse('adminpanel:claim_detail', args=[claim.pk]),
                         {'action': 'refund', 'amount': '20', 'note': 'Partial refund'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, WarrantyClaim.Status.RESOLVED)

    def test_return_visit_must_be_in_the_future(self):
        job = self.book()
        self.quote(job, job.price_p50)
        self.pay(job)
        claim = self.complete(job)
        self.login('adminpanel', EMPLOYEE)
        past = (timezone.localtime() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('adminpanel:claim_detail', args=[claim.pk]), {'action': 'visit', 'when': past})
        claim.refresh_from_db()
        self.assertNotEqual(claim.status, WarrantyClaim.Status.RETURN_VISIT_SCHEDULED)
        future = (timezone.localtime() + timedelta(days=2)).strftime('%Y-%m-%dT%H:%M')
        self.client.post(reverse('adminpanel:claim_detail', args=[claim.pk]), {'action': 'visit', 'when': future})
        self.client.post(reverse('adminpanel:claim_detail', args=[claim.pk]), {'action': 'resolve', 'note': 'Fixed'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, WarrantyClaim.Status.RESOLVED)

    # -- A09 ----------------------------------------------------------------

    def test_deactivated_homeowner_cannot_book(self):
        homeowner = User.objects.get(email=HOMEOWNER)
        self.login('adminpanel', EMPLOYEE)
        self.assertContains(self.client.get(reverse('adminpanel:homeowners') + '?q=goh'), 'Hannah')
        self.client.post(reverse('adminpanel:homeowner_detail', args=[homeowner.pk]),
                         {'action': 'deactivate', 'reason': 'Repeated no-shows'})
        self.login('storefront', HOMEOWNER)
        self.assertRedirects(self.client.get(reverse('storefront:job_request')), reverse('storefront:jobs'))

    # -- A10 ----------------------------------------------------------------

    def test_staff_cancel_in_progress_refunds_everything(self):
        job = self.book()
        self.quote(job, job.price_p50)
        part = self.start_with_flagged_part(job)
        self.login('adminpanel', EMPLOYEE)
        self.assertContains(self.client.get(reverse('adminpanel:jobs') + '?status=IN_PROGRESS'), f'#{job.pk}')
        self.client.post(reverse('adminpanel:job_detail', args=[job.pk]), {'reason': 'Handyman injured'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.CANCELLED)
        paid = job.accepted_quotation.amount + part.price
        self.assertEqual(job.ledger.get(entry_type=Entry.REFUND).amount, paid)
        part.refresh_from_db()
        self.assertNotEqual(part.review_status, PartLine.ReviewStatus.PENDING)
