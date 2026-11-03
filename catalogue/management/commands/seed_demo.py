"""Load the service catalogue, training content and demo accounts.

    python manage.py seed_demo

Safe to run more than once. Every demo account uses DEMO_PASSWORD.
"""
import json
from datetime import time, timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from accounts.models import Address, User
from catalogue.models import Category, JobType, Tier
from handymen.models import Document, HandymanProfile, WeeklyAvailability
from handymen.services import SLOT_START_TIMES

DEMO_PASSWORD = 'fixitfast-demo'

STAFF = [
    ('manager@fixitfast.test', 'Mandy', 'Lim', User.Role.MANAGER),
    ('employee@fixitfast.test', 'Ethan', 'Tan', User.Role.EMPLOYEE),
]
HOMEOWNERS = [
    ('homeowner@fixitfast.test', 'Hannah', 'Goh', '91234567',
     ('Home', 'Blk 123 Tampines Street 11', '#05-67', '521123', 'East')),
]
HANDYMEN = [
    ('standard@fixitfast.test', 'Sam', 'Ng', Tier.STANDARD, 2),
    ('gold@fixitfast.test', 'Gavin', 'Ong', Tier.GOLD, 6),
    ('platinum@fixitfast.test', 'Priya', 'Nair', Tier.PLATINUM, 12),
]
# Weekday daytime (off-peak) plus weekday evening and Saturday (peak) slots.
WEEKDAY_SLOTS = SLOT_START_TIMES
SATURDAY_SLOTS = [time(11), time(14)]


class Command(BaseCommand):
    help = 'Seed the catalogue, Platinum training content and demo accounts.'

    @transaction.atomic
    def handle(self, *args, **options):
        self.seed_catalogue()
        call_command('loaddata', 'training', verbosity=0)
        self.seed_staff()
        self.seed_homeowners()
        self.seed_handymen()
        self.stdout.write(self.style.SUCCESS(f'Done. Demo password for every account: {DEMO_PASSWORD}'))

    def seed_catalogue(self):
        data = json.loads((settings.BASE_DIR / 'ml' / 'catalogue.json').read_text())
        tier_by_label = {label: value for value, label in Tier.choices}
        for row in data['job_types']:
            category, _ = Category.objects.get_or_create(name=row['category'])
            JobType.objects.update_or_create(name=row['name'], defaults={
                'category': category,
                'min_tier': tier_by_label[row['min_tier']],
                'licence_required': row['licence_required'],
                # The model was trained on every job type in catalogue.json, so they can be active.
                'is_active': True,
            })
        self.stdout.write(f'Catalogue: {Category.objects.count()} categories, {JobType.objects.count()} job types')

    def make_user(self, email, first, last, role, mobile=''):
        user, created = User.objects.get_or_create(username=email, defaults={
            'email': email, 'first_name': first, 'last_name': last, 'role': role, 'mobile': mobile,
        })
        if created:
            user.set_password(DEMO_PASSWORD)
            user.save()
        return user

    def seed_staff(self):
        for email, first, last, role in STAFF:
            self.make_user(email, first, last, role)

    def seed_homeowners(self):
        for email, first, last, mobile, (label, street, unit, postal, region) in HOMEOWNERS:
            user = self.make_user(email, first, last, User.Role.HOMEOWNER, mobile)
            Address.objects.get_or_create(homeowner=user, postal_code=postal, defaults={
                'label': label, 'street': street, 'unit': unit,
                'planning_region': region, 'is_default': True,
            })

    def seed_handymen(self):
        for email, first, last, tier, years in HANDYMEN:
            user = self.make_user(email, first, last, User.Role.HANDYMAN)
            profile, created = HandymanProfile.objects.get_or_create(user=user, defaults={
                'tier': tier,
                'years_experience': years,
                'application_status': HandymanProfile.ApplicationStatus.APPROVED,
                'approved_at': timezone.now(),
            })
            if not profile.documents.exists():
                self.seed_documents(profile, tier)
            if not created:
                continue
            job_types = JobType.objects.filter(min_tier__in=Tier.at_or_below(tier))
            if tier == Tier.STANDARD:
                job_types = job_types.filter(licence_required=False)
            profile.job_types.set(job_types)
            for weekday in range(5):
                for start in WEEKDAY_SLOTS:
                    WeeklyAvailability.objects.create(handyman=profile, weekday=weekday, start_time=start)
            for start in SATURDAY_SLOTS:
                WeeklyAvailability.objects.create(handyman=profile, weekday=5, start_time=start)

    def seed_documents(self, profile, tier):
        """Verified documents matching each demo handyman's tier, so licensed job types
        stay selectable on their profile (P02) and their records look real (A05)."""
        expiry = timezone.localdate() + timedelta(days=365)
        docs = [(Document.DocType.IDENTITY, 'NRIC', None)]
        if tier != Tier.STANDARD:
            docs += [
                (Document.DocType.CERTIFICATE, 'WSQ Certificate in Facilities Maintenance',
                 JobType.objects.filter(licence_required=False)),
                (Document.DocType.LICENCE, 'EMA Licensed Electrical Worker',
                 JobType.objects.filter(licence_required=True)),
            ]
        if tier == Tier.PLATINUM:
            docs.append((Document.DocType.INSURANCE, 'Public liability insurance (S$1m)', None))
        for doc_type, title, job_types in docs:
            doc = Document.objects.create(
                handyman=profile, doc_type=doc_type, title=title,
                file=ContentFile(b'Demo document', name=f'{doc_type.lower()}.txt'),
                expiry_date=None if doc_type == Document.DocType.IDENTITY else expiry,
                status=Document.Status.APPROVED, reviewed_at=timezone.now())
            if job_types is not None:
                doc.job_types.set(job_types)
