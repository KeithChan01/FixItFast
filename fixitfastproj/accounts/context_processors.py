def notifications(request):
    """Unread notification count for the navbar badge."""
    if not request.user.is_authenticated:
        return {}
    return {'unread_notifications': request.user.notifications.filter(is_read=False).count()}
