web: python manage.py migrate --noinput && python manage.py import_prototype --noinput --if-empty && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --threads 2 --timeout 60
