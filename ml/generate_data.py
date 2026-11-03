"""Generate a synthetic dataset of completed repair jobs (Section 3.2 of the report).

Each row is one completed job. final_price is the repair amount paid, including
any approved part and excluding the S$4 service fee.

    python ml/generate_data.py            # writes ml/repair_jobs.csv
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
N_RECORDS = 12000
SEED = 2108

# Design assumptions, not observed market data.
TIER_MULTIPLIER = {"Standard": 1.00, "Gold": 1.20, "Platinum": 1.45}
TIMING_MULTIPLIER = {"Off-peak": 1.00, "Peak": 1.25}
REGION_ADJUSTMENT = {"Central": 1.05, "East": 1.00, "North": 0.97, "North-East": 0.98, "West": 0.98}
TIMING_SHARE = {"Off-peak": 0.6, "Peak": 0.4}
PART_PROBABILITY = 0.15
NOISE_SIGMA = 0.15  # log-normal noise so similar jobs vary in price


def main():
    rng = np.random.default_rng(SEED)
    catalogue = json.loads((HERE / "catalogue.json").read_text())
    tiers = catalogue["tiers"]
    regions = catalogue["planning_regions"]
    job_types = catalogue["job_types"]

    rows = []
    for _ in range(N_RECORDS):
        job = job_types[rng.integers(len(job_types))]
        # Only tiers at or above the job type's minimum tier are generated.
        allowed_tiers = tiers[tiers.index(job["min_tier"]):]
        tier = allowed_tiers[rng.integers(len(allowed_tiers))]
        timing_band = rng.choice(list(TIMING_SHARE), p=list(TIMING_SHARE.values()))
        region = regions[rng.integers(len(regions))]

        price = (
            job["base_price"]
            * TIER_MULTIPLIER[tier]
            * TIMING_MULTIPLIER[timing_band]
            * REGION_ADJUSTMENT[region]
            * rng.lognormal(mean=0.0, sigma=NOISE_SIGMA)
        )
        if rng.random() < PART_PROBABILITY:
            price += rng.uniform(10, 0.5 * job["base_price"])

        rows.append({
            "job_type": job["name"],
            "tier": tier,
            "timing_band": timing_band,
            "planning_region": region,
            "final_price": round(price, 2),
        })

    df = pd.DataFrame(rows)
    out = HERE / "repair_jobs.csv"
    df.to_csv(out, index=False)
    print(f"Wrote {len(df)} records to {out}")


if __name__ == "__main__":
    main()
