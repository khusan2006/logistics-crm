"""Local preview settings: a file-based SQLite DB so the app runs with no Postgres.

Inherits everything from the real settings (apps, middleware, templates, auth,
static) and only swaps the database and a couple of dev conveniences. Use with:
`python manage.py runserver --settings=config.settings_dev`.
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "dev.sqlite3",  # noqa: F405
    }
}

# Preview convenience: accept any host the preview proxy uses, and don't let the
# brute-force lockout get in the way of a demo login.
ALLOWED_HOSTS = ["*"]
AXES_ENABLED = False
