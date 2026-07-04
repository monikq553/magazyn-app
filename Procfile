web: python migrate.py && gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60 --access-logfile - app:app
