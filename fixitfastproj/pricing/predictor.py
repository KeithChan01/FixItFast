"""Price range predictions for the slot calendar (H05), and the checks that use them
(P05, P07, A06, A07).

The loaded models are in PricingConfig.models, a dict keyed by percentile:
    {10: Pipeline, 50: Pipeline, 90: Pipeline}

Each Pipeline expects a pandas DataFrame with exactly these columns, using the
same string values as the training data (see ml/catalogue.json):
    job_type         e.g. 'Tap replacement'      -> JobType.name
    tier             'Standard' | 'Gold' | 'Platinum'   (the display label, not Tier.value)
    timing_band      'Off-peak' | 'Peak'          -> TimingBand values
    planning_region  'Central' | 'East' | ...     -> PlanningRegion values
"""
from decimal import Decimal
from typing import NamedTuple

import numpy as np
import pandas as pd
from django.conf import settings

from catalogue.models import Tier, TimingBand
from pricing.apps import PricingConfig

FEATURES = ['job_type', 'tier', 'timing_band', 'planning_region']
PERCENTILES = [10, 50, 90]


class PriceRange(NamedTuple):
    low: Decimal    # 10th percentile: bottom of the range shown to the homeowner
    mid: Decimal    # 50th percentile: reference price shown to the handyman
    high: Decimal   # 90th percentile: top of the range; quotes and parts above it get reviewed


def get_models():
    return PricingConfig.models, PricingConfig.model_version


def known_job_types():
    """Job types the model was trained on. Others must stay inactive (A03)."""
    encoder = PricingConfig.models[50].named_steps['encode']
    return set(encoder.categories_[FEATURES.index('job_type')])


def round_price(value):
    """Round to the nearest settings.PRICE_ROUNDING dollars, e.g. 87.3 -> 85."""
    step = settings.PRICE_ROUNDING
    return Decimal(int(round(value / step)) * step)


def predict_ranges(job_type_name, tier, planning_region):
    """Predict the price range for each timing band (H05).

    Only the timing band differs between slots on the calendar, so the page needs
    just one prediction per band. Returns ({band: PriceRange}, model_version).
    """
    models, version = get_models()
    bands = TimingBand.values
    rows = pd.DataFrame([{
        'job_type': job_type_name,
        'tier': Tier(tier).label,
        'timing_band': band,
        'planning_region': planning_region,
    } for band in bands], columns=FEATURES)

    # One column per percentile, one row per band.
    predictions = np.column_stack([models[p].predict(rows) for p in PERCENTILES])
    # The three models are trained separately and can occasionally cross
    # (10th above 50th), so sort each row before use.
    predictions.sort(axis=1)

    ranges = {
        band: PriceRange(*(round_price(v) for v in row))
        for band, row in zip(bands, predictions)
    }
    return ranges, version


def is_within_range(job, amount):
    """P05: a quotation inside the saved range is released automatically."""
    return job.price_p10 <= amount <= job.price_p90


def exceeds_upper_range(job, revised_total):
    """P07: a part that takes the job total above the saved 90th percentile is flagged."""
    return revised_total > job.price_p90
