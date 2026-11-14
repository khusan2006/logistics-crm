from django.contrib.auth.decorators import login_not_required
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@login_not_required
@never_cache
@require_GET
def healthz(request):
    """For the uptime pinger: 200 when the app can reach its database, 503 when not.
    Reveals nothing beyond that."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 — the answer itself is the report
        return JsonResponse({"status": "error", "db": "unreachable"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
