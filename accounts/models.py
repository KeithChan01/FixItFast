from django.contrib.auth.models import AbstractUser
from django.db import models

from catalogue.models import PlanningRegion


class User(AbstractUser):
    """One account model for every role. The role decides which section a user can enter."""

    class Role(models.TextChoices):
        HOMEOWNER = 'HOMEOWNER', 'Homeowner'
        HANDYMAN = 'HANDYMAN', 'Handyman'
        EMPLOYEE = 'EMPLOYEE', 'Employee'
        MANAGER = 'MANAGER', 'Manager'

    email = models.EmailField(unique=True)
    mobile = models.CharField(max_length=20, blank=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.HOMEOWNER)
    # A deactivated homeowner can still sign in to see their records but cannot book (A09).
    is_deactivated = models.BooleanField(default=False)
    deactivation_reason = models.TextField(blank=True)

    @property
    def is_staff_member(self):
        return self.role in (self.Role.EMPLOYEE, self.Role.MANAGER)

    @property
    def is_manager(self):
        return self.role == self.Role.MANAGER

    def __str__(self):
        return self.get_full_name() or self.email


class Address(models.Model):
    """A homeowner's saved address (H01). planning_region is a pricing model input."""

    homeowner = models.ForeignKey(User, on_delete=models.CASCADE, related_name='addresses')
    label = models.CharField(max_length=50, help_text='e.g. Home, Mum\'s flat')
    street = models.CharField(max_length=200)
    unit = models.CharField(max_length=20, blank=True)
    postal_code = models.CharField(max_length=6)
    planning_region = models.CharField(max_length=10, choices=PlanningRegion.choices)
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = 'addresses'

    def __str__(self):
        unit = f' {self.unit}' if self.unit else ''
        return f'{self.street}{unit}, Singapore {self.postal_code}'


class Notification(models.Model):
    """In-app notification shown in the navbar (H08). Pages update on refresh, not live."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    message = models.CharField(max_length=255)
    link = models.CharField(max_length=255, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.message
