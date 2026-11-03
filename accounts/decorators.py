"""Role-based access for the three sections (A01).

    @role_required(User.Role.EMPLOYEE, User.Role.MANAGER, login_url='adminpanel:login')
    def dashboard(request): ...

The shortcuts below cover the common cases.
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from accounts.models import User


def role_required(*roles, login_url):
    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url)
            if request.user.role not in roles:
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapper
    return decorator


def staff_required(view):
    return role_required(User.Role.EMPLOYEE, User.Role.MANAGER, login_url='adminpanel:login')(view)


def manager_required(view):
    return role_required(User.Role.MANAGER, login_url='adminpanel:login')(view)


def homeowner_required(view):
    return role_required(User.Role.HOMEOWNER, login_url='storefront:login')(view)


def handyman_required(view):
    """Any handyman, including one whose application is still pending (P01 status page)."""
    return role_required(User.Role.HANDYMAN, login_url='partner:login')(view)


def approved_handyman_required(view):
    """Only approved, non-suspended handymen can manage jobs (P02-P07)."""
    @wraps(view)
    @handyman_required
    def wrapper(request, *args, **kwargs):
        profile = request.user.handyman
        if profile.application_status != profile.ApplicationStatus.APPROVED or profile.is_suspended:
            messages.warning(request, 'Your partner account is not active yet.')
            return redirect('partner:home')
        return view(request, *args, **kwargs)
    return wrapper
