"""What each tier requires and offers (Table 1). Shown on H02 and H04; no prices."""
from catalogue.models import Tier

TIER_INFO = {
    Tier.STANDARD: {
        'requirements': 'Approved profile and identity check',
        'job_types': 'Simple jobs',
        'warranty_days': 30,
        'remedy': 'An employee reviews the claim and can refund you',
        'icon': 'bi-person-check',
    },
    Tier.GOLD: {
        'requirements': '15+ completed jobs, 4.3+ rating and a relevant certificate',
        'job_types': 'Most repairs',
        'warranty_days': 90,
        'remedy': 'Free return visit',
        'icon': 'bi-award',
    },
    Tier.PLATINUM: {
        'requirements': 'Everything in Gold, plus FixItFast training and public liability insurance',
        'job_types': 'All job types, including complex repairs',
        'warranty_days': 180,
        'remedy': 'Free return visit, or a replacement Platinum handyman',
        'icon': 'bi-gem',
    },
}


def tier_cards(tiers=None):
    """[(tier value, label, info)] for templates, optionally limited to some tiers."""
    tiers = tiers or Tier.values
    return [(t, Tier(t).label, TIER_INFO[t]) for t in tiers]
