# Start from slim Python image
FROM python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535

ENV DB_NAME=escriptorium \
    DB_USER=escriptorium \
    DB_PASSWORD=escriptorium \
    DB_PORT=5432 \
    SQL_HOST=localhost \
    SQL_PORT=5432 \
    POSTGRES_DB=escriptorium \
    POSTGRES_USER=escriptorium \
    POSTGRES_PASSWORD=escriptorium \
    DEBUG=False \
    SECRET_KEY=please-change-me \
    CSRF_TRUSTED_ORIGINS=http://localhost

# Install system dependencies
# default-jre is needed by the 'jvm' celery queue (pyspark based text alignment)
RUN apt-get update && apt-get install -y \
    build-essential libpq-dev libxml2-dev libxslt-dev zlib1g-dev \
    libffi-dev libssl-dev git curl wget \
    redis postgresql postgresql-contrib \
    libleptonica-dev libvips nano default-jre \
    tesseract-ocr supervisor && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get update && apt-get install -y nodejs \
    && apt-get clean

# Add non privileged user to run the app
RUN addgroup --system uwsgi
RUN adduser --system --no-create-home --ingroup uwsgi uwsgi

# Create app user and working dir
RUN useradd -ms /bin/bash escriptorium
WORKDIR /home/escriptorium

RUN git clone https://gitlab.com/scripta/escriptorium.git && \
    cd escriptorium && \
    git checkout v26.10 && \
    chown escriptorium:escriptorium ../escriptorium -R

# Install Python deps (uWSGI is part of requirements.txt).
# Pin kombu to the celery 5.3.x line: unpinned, pip grabs kombu 5.7 whose
# redis transport requires redis-py>=6.1 while requirements.txt pins
# redis~=4.4.4 — the celery workers then crash-loop with VersionMismatch.
RUN pip install --upgrade "pip<24.1" --no-cache-dir && \
    pip install --no-cache-dir -r ./escriptorium/app/requirements.txt "kombu~=5.3.6" && \
    pip install --no-cache-dir flower requests

# Build the frontend.
# Keep front/dist in place: Django reads front/dist/webpack-stats.json
# (WEBPACK_LOADER config) and `collectstatic` copies the bundles to
# app/static, which is what nginx serves under /static/.
RUN cd /home/escriptorium/escriptorium/front && \
    npm ci && \
    npm run production && \
    npm cache clean --force && \
    chown escriptorium:escriptorium /home/escriptorium/escriptorium/ -R

# Add supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY django-init.sh /django-init.sh
COPY wait-for-db.sh /wait-for-db.sh
COPY import-data.py /import-data.py
COPY export-watcher.py /export-watcher.py
COPY galaxy_runtime.py session_archive.py initialize.py run_stack.py /opt/galaxy/
ENV PYTHONPATH=/opt/galaxy:/home/escriptorium/escriptorium/app \
    CELERY_CONCURRENCY=2 \
    UWSGI_PROCESSES=2
ARG ESCRIPTORIUM_COMMIT=51b2ee62c307bd13eb0f6bad894f124eee7265cc
RUN git -c safe.directory=/home/escriptorium/escriptorium -C /home/escriptorium/escriptorium rev-parse HEAD > /opt/galaxy/upstream-commit && \
    test "$(cat /opt/galaxy/upstream-commit)" = "$ESCRIPTORIUM_COMMIT"

# Galaxy-facing HTTP entry point; backing services stay internal.
EXPOSE 80

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /wait-for-db.sh /django-init.sh

RUN apt-get install -y nginx

# /etc/nginx/conf.d/main.conf
#RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/sites-available/default
# Custom "starting up" page served on 502/503/504 while the stack initializes
COPY nginx/startup.html /usr/share/nginx/html/startup.html

# Optional auth bypass: install the auto-login middleware and hook it into
# settings.py. It only activates when DISABLE_AUTH is truthy ("true"/"1"/"yes"),
# in which case every request is logged in as the bootstrap superuser.
COPY --chown=escriptorium:escriptorium autologin.py /home/escriptorium/escriptorium/app/escriptorium/autologin.py
COPY --chown=escriptorium:escriptorium galaxy_integration.py /home/escriptorium/escriptorium/app/escriptorium/galaxy_integration.py
COPY --chown=escriptorium:escriptorium assets/galaxy-logo.png /home/escriptorium/escriptorium/app/escriptorium/static/images/galaxy-logo.png
COPY docker-settings-patch.py /tmp/docker-settings-patch.py
RUN cat /tmp/docker-settings-patch.py >> /home/escriptorium/escriptorium/app/escriptorium/settings.py && \
    rm /tmp/docker-settings-patch.py

# Collect static files once at build time so nginx serves them immediately.
# Run as the escriptorium user: the
# Django setup also creates app/escriptorium/logs/error.log, which must be
# writable by the runtime user (uwsgi) — as root it would break app startup.
# collectstatic needs no database.
RUN su escriptorium -p -c "cd /home/escriptorium/escriptorium/app && \
    python manage.py collectstatic --noinput"

# The home directory must be traversable for nginx (www-data) to serve
# /static/ from app/static — newer Debian useradd defaults to HOME_MODE 0700,
# which caused 403s on all static content.
RUN chmod 755 /home/escriptorium

# Directories for Galaxy Interactive Tool integration (and standalone use):
# IMPORT_DIR holds files auto-imported at startup, EXPORT_DIR receives
# finished export archives (see import-data.py / export-watcher.py).
RUN mkdir -p /data/import /data/export /data/models /data/project /data/model-export \
    && chown escriptorium:escriptorium /data/import /data/export /data/models /data/project /data/model-export

# Default command
ENTRYPOINT ["docker-entrypoint.sh"]
CMD []
