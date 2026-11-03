"""End-to-end tests of the job flow through all three sections.

    python manage.py test
"""
import io
import shutil
import tempfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from catalogue.management.commands.seed_demo import DEMO_PASSWORD
from catalogue.models import JobType, Tier
from jobs.models import Job, LedgerEntry, PartLine, Quotation, WarrantyClaim

TEMP_MEDIA = tempfile.mkdtemp()
Entry = LedgerEntry.EntryType


def image(name='photo.png'):
    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), 'teal').save(buffer, 'PNG')
    return SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class JobFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)
        cls.tap = JobType.objects.get(name='Tap replacement')

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def login(self, section, email):
        self.client.logout()
        response = self.client.post(reverse(f'{section}:login'), {'username': email, 'password': DEMO_PASSWORD})
        self.assertEqual(response.status_code, 302, f'login failed for {email}')

    # -- helpers that walk the booking flow as each user --------------------

    def book_job(self, tier=Tier.GOLD):
        """H03-H06: request, tier, slot (ML range saved), accept the recommendation."""
        self.login('storefront', 'homeowner@fixitfast.test')
        address = self.client.get(reverse('storefront:job_request')).context['form'].fields['address'].queryset[0]
        response = self.client.post(reverse('storefront:job_request'), {
            'job_type': self.tap.pk, 'address': address.pk,
            'description': 'The kitchen tap drips constantly and the handle is loose.',
            'photos': [image()],
        })
        job = Job.objects.latest('pk')
        self.assertRedirects(response, reverse('storefront:choose_tier', args=[job.pk]))

        self.client.post(reverse('storefront:choose_tier', args=[job.pk]), {'tier': tier})
        page = self.client.get(reverse('storefront:choose_slot', args=[job.pk]))
        day, slots = list(page.context['days'])[0]
        self.client.post(reverse('storefront:choose_slot', args=[job.pk]), {'slot': slots[0]['value']})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.REQUESTED)
        self.assertTrue(job.price_p10 <= job.price_p50 <= job.price_p90)

        self.client.post(reverse('storefront:recommendation', args=[job.pk]), {'action': 'accept'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.HANDYMAN_ASSIGNED)
        return job

    def quote(self, job, amount, justification=''):
        self.login('partner', job.handyman.user.email)
        return self.client.post(reverse('partner:submit_quote', args=[job.pk]), {
            'amount': amount, 'work_description': 'Replace tap cartridge and handle.',
            'justification': justification})

    def pay(self, job):
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:quotation', args=[job.pk]), {'action': 'pay'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.SCHEDULED)

    def start_visit(self, job):
        self.login('partner', job.handyman.user.email)
        self.client.post(reverse('partner:update_progress', args=[job.pk]))   # On the Way
        self.client.post(reverse('partner:update_progress', args=[job.pk]))   # In Progress
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.IN_PROGRESS)

    # -- tests --------------------------------------------------------------

    def test_full_job_with_flagged_part_and_warranty(self):
        job = self.book_job()

        # P05 [ML]: a quotation inside the range is released automatically.
        self.quote(job, job.price_p50)
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.AWAITING_PAYMENT)
        self.assertTrue(job.quotations.get().auto_released)

        # H07: first job, so the service fee is waived.
        self.pay(job)
        self.assertTrue(job.service_fee_waived)

        # P07 [ML]: a part that takes the total above P90 is flagged.
        self.start_visit(job)
        self.client.post(reverse('partner:add_part', args=[job.pk]), {
            'name': 'Premium mixer tap', 'price': '150.00', 'receipt': image('receipt.png')})
        part = job.parts.get()
        self.assertTrue(part.flagged)

        # H09: homeowner approves; P06: completion needs before/after photos.
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:part_decision', args=[part.pk]), {'action': 'approve'})
        self.login('partner', job.handyman.user.email)
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.IN_PROGRESS, 'should not complete without photos')
        for kind in ('BEFORE', 'AFTER'):
            self.client.post(reverse('partner:upload_photos', args=[job.pk]), {'kind': kind, 'images': [image()]})
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.COMPLETED)

        # H10: confirm and rate. The flagged part's payout is held.
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:confirm_job', args=[job.pk]), {'rate': '1', 'rating': 5, 'comment': 'Great'})
        job.refresh_from_db()
        self.assertIsNotNone(job.warranty_ends_at)
        self.assertTrue(job.ledger.filter(entry_type=Entry.PAYOUT_HELD).exists())

        # A07: employee approves the receipt; the part payout is released.
        self.login('adminpanel', 'employee@fixitfast.test')
        self.client.post(reverse('adminpanel:part_detail', args=[part.pk]), {'action': 'approve'})
        part.refresh_from_db()
        self.assertEqual(part.review_status, PartLine.ReviewStatus.APPROVED)
        self.assertEqual(job.ledger.filter(entry_type=Entry.PAYOUT).count(), 2)

        # H11 + A08: Gold claim gets a return visit.
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:raise_claim', args=[job.pk]),
                         {'description': 'The new tap is leaking again from the base.'})
        claim = job.claims.get()
        self.login('adminpanel', 'employee@fixitfast.test')
        self.client.post(reverse('adminpanel:claim_detail', args=[claim.pk]),
                         {'action': 'visit', 'when': '2030-01-07T10:00', 'note': 'Free return visit'})
        claim.refresh_from_db()
        self.assertEqual(claim.status, WarrantyClaim.Status.RETURN_VISIT_SCHEDULED)

    def test_out_of_range_quote_goes_to_review(self):
        job = self.book_job()
        response = self.quote(job, job.price_p90 + 100)          # no justification
        self.assertEqual(response.status_code, 200)              # form re-shown with error
        self.quote(job, job.price_p90 + 100, 'Tap is a rare imported model.')
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.QUOTATION_UNDER_REVIEW)

        # A06: employee releases it.
        quote = job.quotations.get()
        self.login('adminpanel', 'employee@fixitfast.test')
        self.client.post(reverse('adminpanel:quotation_detail', args=[quote.pk]), {'action': 'release'})
        quote.refresh_from_db()
        job.refresh_from_db()
        self.assertEqual(quote.status, Quotation.Status.RELEASED)
        self.assertEqual(job.status, Job.Status.AWAITING_PAYMENT)

    def test_decline_after_start_matches_table_2(self):
        # Use up the first-job waiver so the S$4 fee is charged, as in Table 2.
        first = self.book_job()
        self.quote(first, first.price_p50)
        self.pay(first)
        first.status = Job.Status.COMPLETED
        first.save()

        job = self.book_job()
        self.quote(job, job.price_p50)
        self.pay(job)
        self.start_visit(job)
        self.client.post(reverse('partner:add_part', args=[job.pk]),
                         {'name': 'Valve', 'price': '30', 'receipt': image('r.png')})
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:part_decision', args=[job.parts.get().pk]), {'action': 'decline'})

        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.DECLINED)
        ledger = {e.entry_type: e.amount for e in job.ledger.all()}
        self.assertEqual(ledger[Entry.CONSULTATION_FEE], Decimal('25.00'))
        self.assertEqual(ledger[Entry.REFUND], job.accepted_quotation.amount + Decimal('4.00'))
        # New handymen pay the early 10% commission, so S$2.50 / S$22.50 here (S$3.75 / S$21.25 at 15%).
        self.assertEqual(ledger[Entry.COMMISSION], Decimal('2.50'))
        self.assertEqual(ledger[Entry.PAYOUT], Decimal('22.50'))

    def test_cancel_before_visit_refunds_in_full(self):
        job = self.book_job()
        self.quote(job, job.price_p50)
        self.pay(job)
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:cancel_job', args=[job.pk]), {'reason': 'Fixed it myself'})
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.CANCELLED)
        refund = job.ledger.get(entry_type=Entry.REFUND).amount
        self.assertEqual(refund, job.accepted_quotation.amount)   # fee was waived, so only the quote

    def test_every_page_loads(self):
        job = self.book_job()
        self.quote(job, job.price_p90 + 50, 'Larger job than usual.')
        pages = {
            'storefront': ['storefront:home', 'storefront:services', 'storefront:tiers', 'storefront:jobs',
                           'storefront:account', 'storefront:notifications'],
            'partner': ['partner:home', 'partner:jobs', 'partner:profile', 'partner:availability',
                        'partner:notifications'],
            'adminpanel': ['adminpanel:dashboard', 'adminpanel:catalogue', 'adminpanel:applications',
                           'adminpanel:handymen', 'adminpanel:quotation_queue', 'adminpanel:part_queue',
                           'adminpanel:claims', 'adminpanel:homeowners', 'adminpanel:jobs',
                           'adminpanel:job_type_new', 'adminpanel:notifications'],
        }
        users = {'storefront': 'homeowner@fixitfast.test', 'partner': job.handyman.user.email,
                 'adminpanel': 'manager@fixitfast.test'}
        for section, names in pages.items():
            self.login(section, users[section])
            for name in names:
                self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse('adminpanel:job_detail', args=[job.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('adminpanel:quotation_detail',
                                                 args=[job.quotations.get().pk])).status_code, 200)
        self.assertContains(self.client.get(reverse('adminpanel:dashboard')), 'Revenue this month')

    def test_inactive_job_type_cannot_be_activated_until_model_knows_it(self):
        self.login('adminpanel', 'employee@fixitfast.test')
        response = self.client.post(reverse('adminpanel:job_type_new'), {
            'category': self.tap.category.pk, 'name': 'Aircon servicing', 'min_tier': Tier.STANDARD,
            'is_active': 'on'})
        self.assertFalse(JobType.objects.filter(name='Aircon servicing').exists())
        self.assertContains(response, 'not in the pricing model')
