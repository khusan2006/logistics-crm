web: python manage.py migrate --noinput && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60
