#!/bin/bash
#
# One-shot database initialization, run by supervisord before the app is
# used (program:django-init). Idempotent: safe to re-run on container
# restarts with a persistent postgres data directory.
set -e

export POSTGRES_DB="${POSTGRES_DB:-escriptorium}"
export POSTGRES_USER="${POSTGRES_USER:-escriptorium}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-escriptorium}"

# Bootstrap superuser used for the API (e.g. by import-data.py).
# Override with environment variables when running the container.
BOOTSTRAP_USER="${BOOTSTRAP_USER:-admin}"
BOOTSTRAP_EMAIL="${BOOTSTRAP_EMAIL:-admin@example.com}"
BOOTSTRAP_PASSWORD="${BOOTSTRAP_PASSWORD:-admin}"

echo "Database initialization started."
# Remove a stale init flag so uwsgi (which waits for it) serves no requests
# until migrations are done (relevant on container restarts).
rm -f /tmp/db-initialized

echo "Waiting for postgres to accept connections..."
timeout 120 bash -c 'until pg_isready -q -h localhost; do sleep 1; done'
echo "Postgres is ready."

# Create the application role and database (no-ops if they already exist).
su - postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE USER ${POSTGRES_USER} WITH PASSWORD '${POSTGRES_PASSWORD}';\"" ||
    echo "Role '${POSTGRES_USER}' already exists, skipping."
if su - postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='${POSTGRES_DB}'\"" | grep -q 1; then
    FRESH_DB=0
    echo "Database '${POSTGRES_DB}' already exists."
else
    FRESH_DB=1
    su - postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE DATABASE ${POSTGRES_DB} OWNER ${POSTGRES_USER};\"" ||
        echo "Database '${POSTGRES_DB}' already exists, skipping."
fi

su escriptorium -p -c "cd /home/escriptorium/escriptorium/app/ && python manage.py migrate && python manage.py collectstatic --noinput"

# Create the bootstrap superuser (no-op if it already exists).
# Note: on a fresh database upstream's migration 0004 already creates the
# 'admin' user during migrate, and the later legacy_mode migration fills
# that row with the old default (True), which hides the document dashboard
# behind a "not available in legacy mode" notice. On a fresh database
# therefore, switch the bootstrap user to the new UI; on an existing one the
# user's choice is respected.
su escriptorium -p -c "cd /home/escriptorium/escriptorium/app/ && \
    DJANGO_SUPERUSER_USERNAME='$BOOTSTRAP_USER' \
    DJANGO_SUPERUSER_EMAIL='$BOOTSTRAP_EMAIL' \
    DJANGO_SUPERUSER_PASSWORD='$BOOTSTRAP_PASSWORD' \
    python manage.py createsuperuser --noinput" ||
    echo "Bootstrap user '$BOOTSTRAP_USER' already exists, skipping."
if [ "$FRESH_DB" = "1" ]; then
    su escriptorium -p -c "cd /home/escriptorium/escriptorium/app/ && \
        python manage.py shell -c 'from users.models import User; User.objects.filter(username=\"$BOOTSTRAP_USER\").update(legacy_mode=False)'" ||
        echo "WARNING: could not switch the bootstrap user to the new UI."
fi

echo "Database initialization finished."
touch /tmp/db-initialized
