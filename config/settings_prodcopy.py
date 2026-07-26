"""Run the app against a COPY of the production database.

Points at the throwaway Postgres container holding the restored pg_dump, so the
real Railway database is never touched — nothing done here reaches production.
Everything else (ALLOWED_HOSTS, no login lockout) comes from settings_dev.

    docker start granulalog-rehearsal        # if it is stopped
    python manage.py runserver 127.0.0.1:8010 --settings=config.settings_prodcopy

Rebuild the copy from a fresh dump with:

    railway run --service Postgres sh -c 'pg_dump "$DATABASE_PUBLIC_URL" -Fc -f prod.dump'
    pg_restore -h 127.0.0.1 -p 55432 -U postgres -d granulalog_rehearsal \
        --clean --if-exists --no-owner --no-privileges prod.dump
    python manage.py migrate --settings=config.settings_prodcopy

The container keeps its data while it exists; `docker rm` throws the copy away.
"""

from .settings_dev import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "granulalog_rehearsal",
        "USER": "postgres",
        "PASSWORD": "rehearsal",   # throwaway container, never a real credential
        "HOST": "127.0.0.1",
        "PORT": "55432",
    }
}
