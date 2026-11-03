"""Train the repair price model (Section 3.1 of the report).

Three GradientBoostingRegressor models with quantile loss, at the 10th, 50th and
90th percentiles, each behind a OneHotEncoder in a Pipeline. They are saved
together in price_model.joblib as a dict keyed by percentile: {10: ..., 50: ..., 90: ...}.

    python ml/train_model.py              # reads ml/repair_jobs.csv
"""
import json
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_pinball_loss
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

HERE = Path(__file__).resolve().parent
FEATURES = ["job_type", "tier", "timing_band", "planning_region"]
TARGET = "final_price"
PERCENTILES = [10, 50, 90]
MODEL_VERSION = f"v{date.today():%Y%m%d}"


def build_pipeline(percentile):
    return Pipeline([
        ("encode", OneHotEncoder(handle_unknown="ignore")),
        ("regress", GradientBoostingRegressor(
            loss="quantile",
            alpha=percentile / 100,
            n_estimators=300,
            learning_rate=0.05,
            max_depth=3,
            random_state=2108,
        )),
    ])


def main():
    df = pd.read_csv(HERE / "repair_jobs.csv")
    X, y = df[FEATURES], df[TARGET]
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=2108)

    models, preds = {}, {}
    for p in PERCENTILES:
        model = build_pipeline(p).fit(X_train, y_train)
        models[p] = model
        preds[p] = model.predict(X_test)

    # The three models are trained separately and can cross, so sort per row
    # (the web application does the same before use).
    stacked = np.sort(np.column_stack([preds[p] for p in PERCENTILES]), axis=1)
    p10, p50, p90 = stacked[:, 0], stacked[:, 1], stacked[:, 2]

    coverage = float(np.mean((y_test >= p10) & (y_test <= p90)))
    metrics = {
        "model_version": MODEL_VERSION,
        "records": len(df),
        "train_records": len(X_train),
        "holdout_records": len(X_test),
        "holdout_coverage_p10_p90": round(coverage, 4),
        "holdout_mae_p50": round(float(mean_absolute_error(y_test, p50)), 2),
        "holdout_pinball_loss": {
            str(p): round(float(mean_pinball_loss(y_test, v, alpha=p / 100)), 3)
            for p, v in zip(PERCENTILES, (p10, p50, p90))
        },
    }

    # Refit on all data for the saved model, now that the hold-out check is done.
    final = {p: build_pipeline(p).fit(X, y) for p in PERCENTILES}
    joblib.dump(final, HERE / "price_model.joblib")
    (HERE / "model_info.json").write_text(json.dumps(metrics, indent=2))

    print(json.dumps(metrics, indent=2))
    print(f"Saved models to {HERE / 'price_model.joblib'}")


if __name__ == "__main__":
    main()
