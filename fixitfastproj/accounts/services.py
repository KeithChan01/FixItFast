from accounts.models import Notification, User


def notify(user, message, link=''):
    """Create an in-app notification (H08). Shown in the navbar badge and notifications page."""
    Notification.objects.create(user=user, message=message, link=link)


def notify_staff(message, link=''):
    for user in User.objects.filter(role__in=[User.Role.EMPLOYEE, User.Role.MANAGER], is_active=True):
        notify(user, message, link)
