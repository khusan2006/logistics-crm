"""Daily Telegram digest of overdue and soon-arriving shipments.

Railway cron: run this command once a day, e.g. with the cron schedule
"0 4 * * *" (04:00 UTC) invoking:

    python manage.py send_telegram_digest

Degrades gracefully when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset
(the default) — it prints a notice and returns without any network call.
"""

import json
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import Shipment

NO_OVERDUE_MESSAGE = "Kechikkan yuk yo'q"


def build_digest():
    """Compose the digest message text. Pure function — no I/O, easy to test."""
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)

    overdue = [s for s in Shipment.objects.filter(arrived__isnull=True).select_related(
        "contract", "contract__partner") if s.is_overdue]
    arriving = list(
        Shipment.objects.filter(arrived__isnull=True, eta__in=[today, tomorrow])
        .select_related("contract", "contract__partner")
    )

    if not overdue and not arriving:
        return NO_OVERDUE_MESSAGE

    lines = ["\U0001f69a GranulaLog — kunlik hisobot", ""]

    if overdue:
        lines.append("Kechikkan yuklar:")
        for shipment in overdue:
            contract = shipment.contract
            lines.append(
                f"#{shipment.pk} {contract.brand_summary} · {contract.partner.name} — "
                f"{shipment.days_late} kun kechikdi · "
                f"{shipment.transport}/{shipment.container} · reja: {shipment.eta}"
            )
        lines.append("")

    lines.append("Bugun/ertaga yetib keladi:")
    if arriving:
        for shipment in arriving:
            contract = shipment.contract
            lines.append(
                f"#{shipment.pk} {contract.brand_summary} · {contract.partner.name} · "
                f"{shipment.transport}/{shipment.container} · reja: {shipment.eta}"
            )
    else:
        lines.append("Yo'q")

    return "\n".join(lines)


def send(text):
    """POST the digest to Telegram via stdlib urllib. Never called from tests —
    keeping it a separate function is what lets handle() guard on config
    without any network I/O being reachable when unconfigured."""
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": settings.TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


class Command(BaseCommand):
    help = "Send the daily overdue/arriving-soon shipments digest to Telegram."

    def handle(self, *args, **options):
        text = build_digest()

        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            self.stdout.write("TELEGRAM_BOT_TOKEN/CHAT_ID sozlanmagan — yuborilmadi")
            return

        send(text)
        self.stdout.write(self.style.SUCCESS("Telegram digest yuborildi"))
