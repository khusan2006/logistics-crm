"""Local development settings: the real Postgres, in a container on this machine.

Inherits everything from the real settings — including the database, which is now
the point. Development used to run on a file-based SQLite so the app would start
with no Postgres around; the cost was that the database you developed against and
the database that ships were different engines, and they disagree in places that
matter here (transaction behaviour, concurrent writes, and how a few date and
decimal lookups resolve). Nothing overrides DATABASES any more, so
`POSTGRES_HOST`/`POSTGRES_PORT` from .env point at the container in
docker-compose.yml — and the same settings file, unchanged, points at Railway in
production.

    docker compose up -d --wait
    python manage.py runserver --settings=config.settings_dev

What is left here is only what makes a LOCAL run pleasant, none of it about data.

Not to be confused with config.settings_prodcopy, which inherits this file and
then points at a separate throwaway container holding a restored production dump.
"""

from .settings import *  # noqa: F401,F403

# Preview convenience: accept any host the preview proxy uses, and don't let the
# brute-force lockout get in the way of a demo login.
ALLOWED_HOSTS = ["*"]
AXES_ENABLED = False
