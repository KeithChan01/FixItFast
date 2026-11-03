"""Handyman partner workflow, P01 to P07, including the admin steps it depends on.

    python manage.py test partner
"""
import shutil
import tempfile
from datetime import time, timedelta

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import User
from catalogue.management.commands.seed_demo import DEMO_PASSWORD
from catalogue.models import JobType, Tier
from handymen.models import Document, HandymanProfile, QuizAttempt, QuizQuestion
from jobs.models import Job, LedgerEntry, Recommendation
from jobs.tests import image

TEMP_MEDIA = tempfile.mkdtemp()
Entry = LedgerEntry.EntryType


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class HandymanWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def login(self, section, email, password=DEMO_PASSWORD):
        self.client.logout()
        response = self.client.post(reverse(f'{section}:login'), {'username': email, 'password': password})
        self.assertEqual(response.status_code, 302, f'login failed for {email}')

    def book_for(self, handyman_email, tier=Tier.GOLD, job_type='Tap replacement'):
        """Book a job as the homeowner and accept the given handyman if recommended."""
        self.login('storefront', 'homeowner@fixitfast.test')
        address = User.objects.get(email='homeowner@fixitfast.test').addresses.first()
        self.client.post(reverse('storefront:job_request'), {
            'job_type': JobType.objects.get(name=job_type).pk, 'address': address.pk,
            'description': 'The kitchen tap drips constantly and the handle is loose.'})
        job = Job.objects.latest('pk')
        self.client.post(reverse('storefront:choose_tier', args=[job.pk]), {'tier': tier})
        days = list(self.client.get(reverse('storefront:choose_slot', args=[job.pk])).context['days'])
        self.client.post(reverse('storefront:choose_slot', args=[job.pk]), {'slot': days[0][1][0]['value']})
        self.client.post(reverse('storefront:recommendation', args=[job.pk]), {'action': 'accept'})
        job.refresh_from_db()
        self.assertEqual(job.handyman.user.email, handyman_email)
        return job

    # -- P01 + A04 ----------------------------------------------------------

    def test_apply_reject_resubmit_approve(self):
        response = self.client.post(reverse('partner:apply'), {
            'first_name': 'Ali', 'last_name': 'Rahman', 'email': 'ali@example.com', 'mobile': '91112222',
            'password1': 'Str0ng-pass-123', 'password2': 'Str0ng-pass-123', 'years_experience': 4,
            'job_types': [JobType.objects.get(name='Furniture assembly').pk],
            'identity_document': image('nric.png'),
            'certificate': image('cert.png'), 'certificate_title': 'WSQ Carpentry',
        })
        self.assertRedirects(response, reverse('partner:home'))
        profile = HandymanProfile.objects.get(user__email='ali@example.com')
        self.assertEqual(profile.application_status, HandymanProfile.ApplicationStatus.PENDING)
        self.assertEqual(profile.documents.count(), 2)
        self.assertContains(self.client.get(reverse('partner:home')), 'checking your documents')
        # A pending applicant cannot reach job pages.
        self.assertRedirects(self.client.get(reverse('partner:jobs')), reverse('partner:home'))

        # A04: reject with a reason, which the applicant sees.
        self.login('adminpanel', 'employee@fixitfast.test')
        self.client.post(reverse('adminpanel:decide_application', args=[profile.pk]),
                         {'action': 'reject', 'reason': 'Identity document is blurry.'})
        self.login('partner', 'ali@example.com', 'Str0ng-pass-123')
        self.assertContains(self.client.get(reverse('partner:home')), 'Identity document is blurry.')

        # P01: resubmit with a new document.
        self.client.post(reverse('partner:resubmit'), {
            'doc_type': Document.DocType.IDENTITY, 'title': 'NRIC (clear)', 'file': image('nric2.png'),
            'resubmit': '1'})
        profile.refresh_from_db()
        self.assertEqual(profile.application_status, HandymanProfile.ApplicationStatus.PENDING)

        # A04: approving needs an approved identity document first.
        self.login('adminpanel', 'employee@fixitfast.test')
        self.client.post(reverse('adminpanel:decide_application', args=[profile.pk]), {'action': 'approve'})
        profile.refresh_from_db()
        self.assertEqual(profile.application_status, HandymanProfile.ApplicationStatus.PENDING)
        identity = profile.documents.get(title='NRIC (clear)')
        self.client.post(reverse('adminpanel:review_document', args=[identity.pk]), {'action': 'approve'})
        self.client.post(reverse('adminpanel:decide_application', args=[profile.pk]), {'action': 'approve'})
        profile.refresh_from_db()
        self.assertEqual(profile.application_status, HandymanProfile.ApplicationStatus.APPROVED)
        self.assertEqual(profile.tier, Tier.STANDARD)

        self.login('partner', 'ali@example.com', 'Str0ng-pass-123')
        self.assertContains(self.client.get(reverse('partner:home')), 'Completed jobs')

    # -- P02 ----------------------------------------------------------------

    def test_profile_job_types_respect_tier_and_licence(self):
        self.login('partner', 'standard@fixitfast.test')
        page = self.client.get(reverse('partner:profile'))
        offered = set(page.context['profile_form'].fields['job_types'].queryset.values_list('name', flat=True))
        self.assertIn('Tap replacement', offered)
        self.assertNotIn('Water heater replacement', offered)    # Platinum job
        self.assertNotIn('Power point installation', offered)    # Gold + licence

        # Trying to post a job type above the tier is rejected by the form.
        heater = JobType.objects.get(name='Water heater replacement')
        response = self.client.post(reverse('partner:profile'), {
            'save_profile': '1', 'mobile': '98887777', 'years_experience': 3, 'bio': 'Hi',
            'job_types': [heater.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(HandymanProfile.objects.get(user__email='standard@fixitfast.test')
                         .job_types.filter(pk=heater.pk).exists())

    def test_availability_save_and_time_off(self):
        self.login('partner', 'gold@fixitfast.test')
        profile = HandymanProfile.objects.get(user__email='gold@fixitfast.test')
        self.client.post(reverse('partner:availability'), {'save_weekly': '1', 'slots': ['0-09:00', '5-14:00']})
        self.assertEqual(set(profile.weekly_slots.values_list('weekday', 'start_time')),
                         {(0, time(9)), (5, time(14))})

        next_monday = timezone.localdate() + timedelta(days=(7 - timezone.localdate().weekday()) or 7)
        self.client.post(reverse('partner:availability'), {
            'add_exception': '1', 'date': next_monday.isoformat(), 'start_time': '', 'reason': 'Leave'})
        self.assertTrue(profile.exceptions.filter(date=next_monday, start_time__isnull=True).exists())
        # The slot calendar no longer offers that Monday.
        from handymen.services import free_slots
        days = {timezone.localtime(s).date() for s in free_slots(profile, timezone.localdate(), 14)}
        self.assertNotIn(next_monday, days)

    # -- P03 ----------------------------------------------------------------

    def test_training_quiz_fail_then_wait_then_pass(self):
        self.login('partner', 'gold@fixitfast.test')
        self.assertEqual(self.client.get(reverse('partner:training')).status_code, 200)
        questions = list(QuizQuestion.objects.all())
        wrong = {f'q{q.pk}': 'a' if q.correct_option != 'a' else 'b' for q in questions}
        self.client.post(reverse('partner:quiz'), wrong)
        attempt = QuizAttempt.objects.get()
        self.assertFalse(attempt.passed)
        # Retake is blocked for 24 hours.
        self.assertRedirects(self.client.get(reverse('partner:quiz')), reverse('partner:training'))
        QuizAttempt.objects.update(taken_at=timezone.now() - timedelta(hours=25))
        right = {f'q{q.pk}': q.correct_option for q in questions}
        self.client.post(reverse('partner:quiz'), right)
        self.assertTrue(QuizAttempt.objects.filter(passed=True).exists())

        # Standard handymen cannot see the training.
        self.login('partner', 'standard@fixitfast.test')
        self.assertRedirects(self.client.get(reverse('partner:training')), reverse('partner:home'))

    # -- P04 ----------------------------------------------------------------

    def test_decline_passes_job_on_and_address_hidden_until_paid(self):
        job = self.book_for('gold@fixitfast.test')
        self.login('partner', 'gold@fixitfast.test')
        page = self.client.get(reverse('partner:job_detail', args=[job.pk]))
        self.assertContains(page, 'full address shown after payment')
        self.assertNotContains(page, job.address.street)

        self.client.post(reverse('partner:decline_job', args=[job.pk]), {'reason': 'Out of town'})
        job.refresh_from_db()
        self.assertIsNone(job.handyman)
        self.assertEqual(job.status, Job.Status.REQUESTED)
        self.assertEqual(job.recommendations.get(handyman__user__email='gold@fixitfast.test').status,
                         Recommendation.Status.HANDYMAN_DECLINED)
        # The declined job is no longer visible to that handyman.
        self.assertEqual(self.client.get(reverse('partner:job_detail', args=[job.pk])).status_code, 404)

    def test_quote_deadline_expiry(self):
        job = self.book_for('gold@fixitfast.test')
        Job.objects.filter(pk=job.pk).update(quote_deadline=timezone.now() - timedelta(minutes=1))
        from jobs.services import housekeeping
        housekeeping()
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.REQUESTED)
        self.assertIsNone(job.handyman)

    # -- P05 to P07 and payout -----------------------------------------------

    def test_visit_progress_part_and_payout(self):
        job = self.book_for('gold@fixitfast.test')
        self.login('partner', 'gold@fixitfast.test')
        self.client.post(reverse('partner:submit_quote', args=[job.pk]),
                         {'amount': job.price_p50, 'work_description': 'Replace cartridge.'})
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:quotation', args=[job.pk]), {'action': 'pay'})

        self.login('partner', 'gold@fixitfast.test')
        self.assertContains(self.client.get(reverse('partner:job_detail', args=[job.pk])), job.address.street)
        # P07 is not available before the visit starts.
        self.client.post(reverse('partner:add_part', args=[job.pk]),
                         {'name': 'Washer', 'price': '5', 'receipt': image()})
        self.assertFalse(job.parts.exists())

        for expected in (Job.Status.ON_THE_WAY, Job.Status.IN_PROGRESS):
            self.client.post(reverse('partner:update_progress', args=[job.pk]))
            job.refresh_from_db()
            self.assertEqual(job.status, expected)

        # A small part stays within range and is not flagged.
        self.client.post(reverse('partner:add_part', args=[job.pk]),
                         {'name': 'Washer', 'price': '5', 'receipt': image()})
        part = job.parts.get()
        self.assertFalse(part.flagged)
        # Cannot complete while the homeowner has not decided on the part, or without photos.
        response = self.client.post(reverse('partner:update_progress', args=[job.pk]), follow=True)
        self.assertContains(response, 'Wait for the homeowner')
        self.assertContains(response, 'photo from before')

        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:part_decision', args=[part.pk]), {'action': 'approve'})
        self.login('partner', 'gold@fixitfast.test')
        self.client.post(reverse('partner:upload_photos', args=[job.pk]),
                         {'kind': 'BEFORE', 'images': [image('b1.png'), image('b2.png')]})
        self.client.post(reverse('partner:upload_photos', args=[job.pk]), {'kind': 'AFTER', 'images': [image('a.png')]})
        self.assertEqual(job.photos.count(), 3)
        self.client.post(reverse('partner:update_progress', args=[job.pk]))
        job.refresh_from_db()
        self.assertEqual(job.status, Job.Status.COMPLETED)

        # Homeowner confirms; handyman sees payout at the early 10% rate.
        self.login('storefront', 'homeowner@fixitfast.test')
        self.client.post(reverse('storefront:confirm_job', args=[job.pk]), {'rate': '1', 'rating': 4})
        gross = job.accepted_quotation.amount + 5
        payout = job.ledger.get(entry_type=Entry.PAYOUT).amount
        self.assertEqual(payout, gross - (gross * 10 / 100).quantize(payout))
        self.login('partner', 'gold@fixitfast.test')
        page = self.client.get(reverse('partner:job_detail', args=[job.pk]))
        self.assertContains(page, 'Your earnings')
        self.assertContains(self.client.get(reverse('partner:home')), 'Paid out this month')

    def test_suspended_handyman_is_blocked_and_not_recommended(self):
        profile = HandymanProfile.objects.get(user__email='gold@fixitfast.test')
        self.login('adminpanel', 'employee@fixitfast.test')
        self.client.post(reverse('adminpanel:handyman_action', args=[profile.pk]),
                         {'action': 'suspend', 'reason': 'Complaint under investigation'})
        self.login('partner', 'gold@fixitfast.test')
        self.assertRedirects(self.client.get(reverse('partner:jobs')), reverse('partner:home'))
        from handymen.services import eligible_handymen
        self.assertFalse(eligible_handymen(JobType.objects.get(name='Tap replacement'), Tier.GOLD).exists())
