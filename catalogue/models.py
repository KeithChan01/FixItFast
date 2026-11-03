from django.db import models


class Tier(models.TextChoices):
    STANDARD = 'STANDARD', 'Standard'
    GOLD = 'GOLD', 'Gold'
    PLATINUM = 'PLATINUM', 'Platinum'

    @classmethod
    def rank(cls, tier):
        """0 for Standard, 1 for Gold, 2 for Platinum, so tiers can be compared."""
        return cls.values.index(tier)

    @classmethod
    def at_or_above(cls, tier):
        return cls.values[cls.rank(tier):]

    @classmethod
    def at_or_below(cls, tier):
        return cls.values[:cls.rank(tier) + 1]


class TimingBand(models.TextChoices):
    # Off-peak: weekdays 9 a.m.-6 p.m. Peak: weekdays 6-10 p.m. and weekends (H05).
    OFF_PEAK = 'Off-peak', 'Off-peak'
    PEAK = 'Peak', 'Peak'


class PlanningRegion(models.TextChoices):
    # Values match the training data, so they can be passed straight to the model.
    CENTRAL = 'Central', 'Central'
    EAST = 'East', 'East'
    NORTH = 'North', 'North'
    NORTH_EAST = 'North-East', 'North-East'
    WEST = 'West', 'West'


class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name_plural = 'categories'

    def __str__(self):
        return self.name


class JobType(models.Model):
    """A specific repair within a category (A03). Its name is a pricing model input."""

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name='job_types')
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    min_tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.STANDARD)
    licence_required = models.BooleanField(default=False)
    # A new job type stays inactive until the model has been retrained to include it.
    is_active = models.BooleanField(default=False)

    class Meta:
        ordering = ['category__name', 'name']

    def allowed_tiers(self):
        return Tier.at_or_above(self.min_tier)

    def __str__(self):
        return self.name
