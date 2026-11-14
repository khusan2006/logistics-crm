"""Send a message to the developer's monitoring chat.

Separate from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, which belong to the client's
own digest. Reads MONITOR_TELEGRAM_BOT_TOKEN / MONITOR_TELEGRAM_CHAT_ID; when either
is blank, nothing is sent.

Failures are written to stderr (so they reach the Railway logs) rather than to
`logging`: a failed alert logged at ERROR would trigger another alert."""

import json
import sys
import threading
import urllib.request

from django.conf import settings

MAX_LENGTH = 4000  # Telegram's limit is 4096 characters


def is_configured():
    return bool(settings.MONITOR_TELEGRAM_BOT_TOKEN and settings.MONITOR_TELEGRAM_CHAT_ID)


def send_message(text):
    """Send `text` now. Returns True on success, False when unconfigured or on failure."""
    if not is_configured():
        return False
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH] + "\n…"
    url = f"https://api.telegram.org/bot{settings.MONITOR_TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": settings.MONITOR_TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status == 200
    except Exception as exc:  # noqa: BLE001 — reported to stderr, see module docstring
        sys.stderr.write(f"[monitoring] Telegram send failed: {exc!r}\n")
        return False


def send_in_background(text):
    """Send without blocking the request that hit the error. Not a daemon thread, so
    a management command that errors still delivers its alert before exiting."""
    threading.Thread(target=send_message, args=(text,), name="monitor-alert").start()
