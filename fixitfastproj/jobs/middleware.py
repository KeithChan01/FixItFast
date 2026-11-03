import time

from jobs.services import housekeeping


class HousekeepingMiddleware:
    """Run the time-based job rules (quote deadlines, auto-confirm) at most once a minute.

    This avoids needing a separate scheduler such as cron or Celery for the project.
    """

    INTERVAL_SECONDS = 60
    last_run = 0.0

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        now = time.monotonic()
        if now - HousekeepingMiddleware.last_run > self.INTERVAL_SECONDS:
            HousekeepingMiddleware.last_run = now
            housekeeping()
        return self.get_response(request)
