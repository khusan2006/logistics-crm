from django.db import models


class ErrorEvent(models.Model):
    """One kind of error, grouped by fingerprint (exception type + where it was raised).

    Each new occurrence bumps `count` and `last_seen` instead of adding a row, so the
    table stays small. `last_alerted` throttles Telegram: one alert per kind per hour."""

    fingerprint = models.CharField(max_length=40, unique=True)
    title = models.CharField(max_length=300)
    location = models.CharField(max_length=300, blank=True)
    logger = models.CharField(max_length=100, blank=True)
    last_path = models.CharField(max_length=300, blank=True)
    count = models.PositiveIntegerField(default=1)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    last_alerted = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]

    def __str__(self):
        return f"{self.title} ({self.count}x)"
