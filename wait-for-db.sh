#!/bin/bash
# Wait for the database initialization (django-init.sh) to finish before
# starting an application process that imports the Django app (uwsgi, celery).
# django-init.sh touches /tmp/db-initialized once migrations are done.
until [ -f /tmp/db-initialized ]; do
    sleep 1
done
exec "$@"
