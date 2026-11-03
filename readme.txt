FixItFast - IS2108 Pair Project
================================

Group number: PPXX
Members (leader marked *):
  * [Full name], [A0000000X], [email], [contact number]
    [Full name], [A0000000X], [email], [contact number]
Demo video: [YouTube link]


How to run
----------
Requires Python 3.13.

    python -m venv .venv
    .venv\Scripts\activate            (Windows)   or   source .venv/bin/activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py seed_demo
    python manage.py runserver

Sections:
    Storefront (homeowners)      http://127.0.0.1:8000/
    Partner section (handymen)   http://127.0.0.1:8000/partner/
    Admin panel (employees)      http://127.0.0.1:8000/staff/

Demo accounts (password for all: fixitfast-demo)
    manager@fixitfast.test       Manager (sees revenue)
    employee@fixitfast.test      Employee
    homeowner@fixitfast.test     Homeowner, address in Tampines (East)
    standard@fixitfast.test      Standard handyman
    gold@fixitfast.test          Gold handyman
    platinum@fixitfast.test      Platinum handyman

Users sign in with their email address, which is also stored as the username.
When creating users (H01, P01), set username=email.


Pricing model
-------------
The model files are already in ml/. To regenerate them:

    python ml/generate_data.py
    python ml/train_model.py

scikit-learn is pinned to 1.8.0 in requirements.txt because joblib model
files may not load under a different version.


Project layout
--------------
config/       settings (business rules and fees at the bottom), root URLs
accounts/     User (with role), Address, Notification, role decorators, login view
catalogue/    Tier, TimingBand, PlanningRegion, Category, JobType, postal-region lookup, seed_demo
handymen/     HandymanProfile, Document, TierChange, availability, training and quiz
jobs/         Job, photos, timeline, Recommendation, Quotation, PartLine, LedgerEntry,
              Review, WarrantyClaim
pricing/      loads price_model.joblib once at startup; predictor.py for the ML helpers
storefront/   homeowner views and templates
partner/      handyman views and templates
adminpanel/   employee views and templates (the default Django admin is not used)
templates/    shared base layout and partials
ml/           synthetic data generator, training script, saved model


Tests
-----
    python manage.py test

Runs the job flow end to end through all three sections: booking with the ML
range, auto-release and staff review of quotations, flagged parts, decline
settlement (Table 2), cancellation refunds, warranty claims and page loads.


Where each user story lives
---------------------------
Business rules are in the services modules; views stay thin.

    jobs/services.py         booking, matching, quotations, parts, payments, warranty
    handymen/services.py     availability, timing bands, recommendation score, tier rules
    pricing/predictor.py     ML predictions (H05) and range checks (P05, P07)
    jobs/middleware.py       quote deadlines (P04) and 48-hour auto-confirm (H10)

    Storefront   storefront/views.py   H01-H11
    Partner      partner/views.py      P01-P07
    Admin panel  adminpanel/views.py   A02-A10   (A01 login/roles: accounts/)

ML stories: H05 predicts and saves the range; P05 auto-releases quotations
inside it; A06 reviews those outside it; P07/A07 flag and check parts above
the 90th percentile; A02 shows the share released automatically.
