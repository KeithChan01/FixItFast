import json

import joblib
from django.apps import AppConfig
from django.conf import settings


class PricingConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pricing'

    # Filled once in ready(): {10: Pipeline, 50: Pipeline, 90: Pipeline}
    models = None
    model_version = ''

    def ready(self):
        """Load the three quantile models once when Django starts, not on every request (Section 3.3)."""
        PricingConfig.models = joblib.load(settings.PRICE_MODEL_PATH)
        info = json.loads(settings.PRICE_MODEL_INFO_PATH.read_text())
        PricingConfig.model_version = info['model_version']
