# Render deployment verification checklist

This repository is ready for Render deployment, but this execution environment has no Git remote, no GitHub credentials, and no Render API/session token. Because of that, the agent cannot push commits, trigger a Render deploy, or read Render logs from here.

## Verified local repository state

- `requirements.txt` contains all runtime libraries used by the application.
- `render.yaml` installs dependencies with `pip install --upgrade pip && pip install -r requirements.txt` for both the web service and backup cron.
- `render.yaml` starts the web service with `python migrate.py && gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60 --access-logfile - app:app`.
- `Procfile` uses the same migration + Gunicorn startup command.
- `.python-version` and `render.yaml` are aligned to Python `3.13.13`.
- The health check path is `/health`.

## Required Render environment variables

Set these on the web service:

- `SECRET_KEY` — generated or manually configured.
- `DATABASE_URL` — PostgreSQL connection string.
- `FIREBASE_SERVICE_ACCOUNT_JSON` — full Firebase service account JSON.
- `FIREBASE_API_KEY`
- `FIREBASE_AUTH_DOMAIN`
- `FIREBASE_PROJECT_ID`
- `FIREBASE_STORAGE_BUCKET`
- `FIREBASE_MESSAGING_SENDER_ID`
- `FIREBASE_APP_ID`
- `FIREBASE_MEASUREMENT_ID`
- `ADMIN_EMAILS` — comma-separated administrator email addresses.
- `BACKUP_ENCRYPTION_KEY` — required for encrypted backups.
- `ALLOW_BACKUP_RESTORE=false` by default.
- `DB_POOL_SIZE=5` for the web service.
- `RATELIMIT_STORAGE_URI` — optional; defaults to `memory://`.

Set these on the cron backup service:

- `DATABASE_URL`
- `FIREBASE_SERVICE_ACCOUNT_JSON`
- Firebase web/storage variables used by backup integration.
- `BACKUP_ENCRYPTION_KEY`
- `DB_POOL_SIZE=2`

## Expected Render build commands

Render should run:

```bash
pip install --upgrade pip && pip install -r requirements.txt
```

Expected result: all packages from `requirements.txt` install successfully from PyPI. If this fails on Render, check the Render build log for the first package error and update `requirements.txt` only if a real version conflict is reported.

## Expected Render start commands

Render should run:

```bash
python migrate.py && gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 60 --access-logfile - app:app
```

Expected result:

1. Database migrations finish without an exception.
2. Gunicorn starts and binds to `$PORT`.
3. `GET /health` returns HTTP 200 with `{"status":"ok"}`.

## Deployment steps to run from a workstation with GitHub + Render access

```bash
git status
git log --oneline -5
git push origin HEAD:main
```

Then in Render:

1. Open the `magazyn-app` web service.
2. Confirm a new deploy started from the pushed commit.
3. Watch the build logs until dependency installation completes.
4. Watch startup logs until Gunicorn is serving traffic.
5. Open `/health` and confirm HTTP 200.
6. Open `/login` and confirm the login page loads.
7. Run the smoke tests listed in `RUNNING.md`.

## Render log triage

If deployment fails, do not treat the deployment as complete. Use the first failing log section:

- Dependency failure: fix `requirements.txt`, commit, push again.
- Migration failure: fix `run_db_migrations()` or database constraints, commit, push again.
- Firebase failure: verify `FIREBASE_SERVICE_ACCOUNT_JSON` and web config variables.
- Database failure: verify `DATABASE_URL`, network access, SSL mode, and internal Render database hostname fallback.
- Runtime import failure: add the missing dependency to `requirements.txt`, commit, push again.

## Final report fields after a successful Render deploy

After Render is green, report:

- Application URL.
- Login URL, usually `<application-url>/login`.
- Render deploy ID/commit SHA.
- Confirmation that `/health` returned HTTP 200.
- Confirmation that the manual smoke tests in `RUNNING.md` passed.
