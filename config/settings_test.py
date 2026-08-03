"""Test settings: isolated from the real Postgres data.

Overrides the database with a throwaway file-based SQLite so the E2E/permission
suite never touches development or production data. Everything else (apps,
middleware, templates, auth) is inherited from the real settings, so the tests
exercise the same stack the app runs on.
"""

from .settings import *  # noqa: F401,F403

# A self-contained SQLite database, recreated per test run. File-based (not
# ":memory:") so Django's LiveServerTestCase thread and the test thread share it.
#
# TEST_DB_SUFFIX lets several pytest processes run at once without fighting over
# one file: each exports its own suffix and gets its own database. Unset — which
# is every normal run — the name is exactly what it has always been.
import os  # noqa: E402

_db_suffix = os.environ.get("TEST_DB_SUFFIX", "")
_db_path = BASE_DIR / f"test_db{_db_suffix}.sqlite3"  # noqa: F405

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": _db_path,
        "TEST": {"NAME": _db_path},
    }
}

# django-axes lockout has no place in tests — a correct login must never be
# throttled by a previous run's counters.
AXES_ENABLED = False

# Fast, deterministic password hashing for the test users.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# The Playwright live server binds to an ephemeral host/port; allow anything.
ALLOWED_HOSTS = ["*"]
