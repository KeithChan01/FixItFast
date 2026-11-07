FixItFast - IS2108 Pair Project
================================

FixItFast is a B2C home-repair matching platform built with Django. A homeowner
describes a repair once, chooses a service tier (Standard, Gold or Platinum),
and picks an appointment slot from a calendar that shows a machine-learning
price range beside every slot. FixItFast then recommends one handyman, holds
the payment until the job is done, and handles parts, ratings and warranty
claims.

The web application has three sections:

    Storefront (homeowners)      http://127.0.0.1:8000/
    Partner section (handymen)   http://127.0.0.1:8000/partner/
    Admin panel (employees)      http://127.0.0.1:8000/staff/

Each section has its own login page. The default Django admin is not used.


How to run
----------
Requires Python 3.13. From this folder (the one containing this readme):

    python -m venv .venv
    .venv\Scripts\activate                 (Windows)
    source .venv/bin/activate              (macOS / Linux)
    pip install -r requirements.txt
    cd fixitfastproj
    python manage.py migrate
    python manage.py seed_demo
    python manage.py runserver

Then open http://127.0.0.1:8000/.

seed_demo loads the service catalogue, the Platinum training modules and quiz,
and the demo accounts below. It is safe to run more than once.

To start again from a clean database, stop the server, delete
fixitfastproj/db.sqlite3 and fixitfastproj/media/, then run migrate and
seed_demo again.


Demo accounts
-------------
Password for every account: fixitfast-demo


    Admin panel  http://127.0.0.1:8000/staff/login/   manager@fixitfast.test     Manager (also sees monthly revenue)
                                                      employee@fixitfast.test    Employee

    Storefront   http://127.0.0.1:8000/login/         homeowner@fixitfast.test   Homeowner with an address in Tampines (East)

    Partner      http://127.0.0.1:8000/partner/login/ standard@fixitfast.test    Standard handyman
                                                      gold@fixitfast.test        Gold handyman
                                                      platinum@fixitfast.test    Platinum handyman

Sign in with the email address. New homeowners can register at /register/ and
new handymen can apply at /partner/apply/.

Notes for the demo:
  - There is one handyman per tier, so "Show me another handyman" (H06) finds
    no one else and asks the homeowner to choose another slot or tier.
  - The seeded Gold and Platinum handymen do not meet the real tier
    requirements, so the admin panel flags them "Below tier requirements".
    They exist so every tier can be demonstrated.
  - Handymen offer two-hour slots at 9 a.m., 11 a.m., 2 p.m., 4 p.m., 6 p.m.
    and 8 p.m. Weekday slots before 6 p.m. are off-peak; evenings and weekends
    are peak.
  - New handymen pay the reduced 10% commission on their first 20 jobs, so
    demo settlements use 10% rather than the standard 15%.


Machine learning model
----------------------
One model is used: a repair price model in ml/price_model.joblib. It is three
scikit-learn GradientBoostingRegressor pipelines (quantile loss at the 10th,
50th and 90th percentiles), each with a OneHotEncoder, saved as a dictionary
keyed by percentile.

    Inputs     job type, tier, timing band (off-peak / peak), planning region
    Output     10th percentile  - bottom of the range shown to the homeowner
               50th percentile  - reference price shown to the handyman
               90th percentile  - top of the range

Training data is a synthetic dataset of 12,000 completed jobs
(ml/repair_jobs.csv). On a 20% hold-out set, 78.3% of jobs fell inside the
predicted 10th-90th percentile range.

The models are loaded once when Django starts (pricing/apps.py). The
prediction is made when the homeowner opens the slot calendar and saved on the
job when a slot is chosen, so the range cannot change during the job. It is
then used to automate two decisions:

    - A quotation inside the range is released to the homeowner automatically;
      one outside it goes to the staff review queue.
    - A replacement part that takes the job total above the 90th percentile is
      flagged, and its payout is held until an employee checks the receipt.

The model files are included. To regenerate them, from fixitfastproj/:

    python ml/generate_data.py
    python ml/train_model.py

scikit-learn is pinned to 1.8.0 in requirements.txt because joblib model
files may not load under a different version.


User stories
------------
Admin panel (adminpanel/views.py; A01 login and roles in accounts/)
    A01 Employee login and role-based access
    A02 Operations dashboard and launch measures
    A03 Service catalogue and minimum tiers
    A04 Verify handyman applications
    A05 Handyman records and tiers
    A06 Review out-of-range quotations          [ML]
    A07 Review flagged part lines               [ML]
    A08 Warranty claims
    A09 Homeowner records
    A10 Jobs and payment records

Storefront (storefront/views.py)
    H01 Register and manage account
    H02 Browse services and compare tiers
    H03 Submit a repair request
    H04 Choose a service tier
    H05 Choose a slot with a predicted price range   [ML]
    H06 Review the recommended handyman
    H07 Accept the quotation and pay
    H08 Track, cancel and review past jobs
    H09 Approve an added part or decline the repair
    H10 Confirm completion and rate the handyman
    H11 Raise a warranty claim

Partner section (partner/views.py)
    P01 Apply as a handyman
    P02 Manage profile, job types and availability
    P03 Complete Platinum training
    P04 Receive job requests
    P05 Submit a quotation with a reference price    [ML]
    P06 Update job progress
    P07 Add a replacement part                       [ML]

Payments are simulated: paying records entries in each job's payment ledger,
and no payment gateway is used.


Project layout
--------------
    fixitfast/                 this folder
        readme.txt
        requirements.txt
        fixitfastproj/         the Django project (contains manage.py)

Inside fixitfastproj/, business rules live in the services modules, so the
views stay short.

    fixitfastproj/ settings (fees and business rules at the bottom), root URLs,
                   wsgi.py and asgi.py
    accounts/      User with role, Address, Notification, login and role decorators
    catalogue/     Tier, TimingBand, PlanningRegion, Category, JobType,
                   postal code to region lookup, seed_demo command
    handymen/      HandymanProfile, Document, TierChange, availability,
                   training and quiz; services.py for availability, timing
                   bands, recommendation score and tier eligibility
    jobs/          Job, photos, timeline, Recommendation, Quotation, PartLine,
                   LedgerEntry, Review, WarrantyClaim; services.py for the job
                   lifecycle; middleware.py for quote deadlines and the
                   48-hour auto-confirm
    pricing/       loads the model at startup; predictor.py for predictions
                   and range checks
    storefront/    homeowner views and templates
    partner/       handyman views and templates
    adminpanel/    employee views and templates
    templates/     shared base layout and partials
    static/        stylesheet (Bootstrap 5 is loaded from a CDN)
    ml/            data generator, training script, dataset and saved model

JavaScript is limited to Bootstrap's own bundle for the navbar, dropdowns and
accordions. All pricing and business logic runs on the server.


Tests
-----
From fixitfastproj/:

    python manage.py test

27 tests across the apps, including end-to-end runs of the job flow through
all three sections: booking with the ML range, automatic release and staff
review of quotations, flagged parts, decline settlement, cancellation
refunds, warranty claims, and loading every page.
