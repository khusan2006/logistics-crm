"""GranulaLog's own sections of the daily monitoring report
(settings.MONITOR_REPORT_SECTIONS, see monitoring/report.py)."""

from collections import Counter
from datetime import timedelta

from django.utils import timezone

from crm.models import AuditLog, Scale

SCALE_QUIET_AFTER = timedelta(hours=24)


def activity(since):
    rows = list(AuditLog.objects.filter(created_at__gte=since).values_list("action", "user_id"))
    if not rows:
        return ["📝 Activity: nothing recorded in the last 24h"]
    by_action = Counter(action for action, _ in rows)
    users = len({user for _, user in rows if user})
    labels = dict(AuditLog.Action.choices)
    parts = ", ".join(f"{labels.get(a, a)} {n}" for a, n in by_action.most_common())
    return [f"📝 Activity: {len(rows)} actions by {users} users ({parts})"]


def scales(since):
    active = list(Scale.objects.filter(is_active=True))
    if not active:
        return []
    now = timezone.now()
    lines = ["⚖️ Scales:"]
    for scale in active:
        if scale.online:
            state = "online"
        elif scale.last_seen and now - scale.last_seen < SCALE_QUIET_AFTER:
            state = f"offline, last data {timezone.localtime(scale.last_seen):%H:%M}"
        else:
            state = "⚠️ no data for 24h+"
        lines.append(f"• {scale.name}: {state}")
    return lines
