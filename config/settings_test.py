"""Test settings: isolated from the real Postgres data.

Overrides the database with a throwaway file-based SQLite so the E2E/permission
suite never touches development or production data. Everything else (apps,
middleware, templates, auth) is inherited from the real settings, so the tests
exercise the same stack the app runs on.
"""

from .settings import *  # noqa: F401,F403

# A self-contained SQLite database, recreated per test run. File-based (not
# ":memory:") so Django's LiveServerTestCase thread and the test thread share it.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "test_db.sqlite3",  # noqa: F405
        "TEST": {"NAME": BASE_DIR / "test_db.sqlite3"},  # noqa: F405
    }
}

# django-axes lockout has no place in tests — a correct login must never be
# throttled by a previous run's counters.
AXES_ENABLED = False

# Fast, deterministic password hashing for the test users.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The Playwright live server binds to an ephemeral host/port; allow anything.
ALLOWED_HOSTS = ["*"]
