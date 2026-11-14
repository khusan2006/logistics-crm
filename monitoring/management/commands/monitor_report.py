"""Send the daily health report to the developer's Telegram.

Runs as its own Railway cron service (PROCESS=report, see bin/start), e.g. schedule
"0 4 * * *" = 09:00 Tashkent. The report arrives every day, errors or not, so a
missing report is itself a sign something is wrong.

    python manage.py monitor_report --dry-run   # print instead of sending
"""

from django.core.management.base import BaseCommand, CommandError

from monitoring import telegram
from monitoring.report import build_report, prune_old_errors


class Command(BaseCommand):
    help = "Send the daily monitoring report to Telegram."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print, don't send.")

    def handle(self, *args, dry_run=False, **options):
        text = build_report()
        prune_old_errors()

        if dry_run:
            self.stdout.write(text)
            return
        if not telegram.is_configured():
            raise CommandError("MONITOR_TELEGRAM_BOT_TOKEN / MONITOR_TELEGRAM_CHAT_ID are not set")
        if not telegram.send_message(text):
            raise CommandError("Telegram send failed (see stderr above)")
        self.stdout.write(self.style.SUCCESS("Monitoring report sent"))
