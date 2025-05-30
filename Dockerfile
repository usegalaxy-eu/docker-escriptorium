# Start from slim Python image
FROM python:3.11-slim

ENV DB_NAME=escriptorium \
    DB_USER=escriptorium \
    DB_PASSWORD=escriptorium \
    DB_PORT=5432

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential libpq-dev libxml2-dev libxslt-dev zlib1g-dev \
    libffi-dev libssl-dev git curl wget \
    redis postgresql postgresql-contrib \
    libleptonica-dev libvips nano default-jre default-jdk ant \
    tesseract-ocr supervisor && \
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get update && apt-get install -y nodejs \
    && apt-get clean

# Set JAVA_HOME
#RUN export JAVA_HOME=$(dirname $(dirname $(readlink -f  /usr/bin/java)))

# Add non privileged user to run the app
RUN addgroup --system uwsgi
RUN adduser --system --no-create-home --ingroup uwsgi uwsgi

# Create app user and working dir
RUN useradd -ms /bin/bash escriptorium
WORKDIR /home/escriptorium

RUN git clone https://gitlab.com/scripta/escriptorium.git && \
    cd escriptorium && \
    git checkout v0.13.8b-hotfixes5 && \
    chown escriptorium:escriptorium ../escriptorium -R 
    ## && \
    ## rm -rf .git

# Install Python deps
RUN pip install --upgrade "pip<24.1" --no-cache-dir && \
    pip install --no-cache-dir -r ./escriptorium/app/requirements.txt && \
    pip install kraken flower gunicorn psycopg2 --no-cache-dir

#ADD webpack.common.js /home/escriptorium/escriptorium/front/webpack.common.js
#RUN sed -i '1 s/^.*$/window.Vue = require("vue");/' /home/escriptorium/escriptorium/front/src/editor/mixins.js 
RUN cd /home/escriptorium/escriptorium/front && \
    npm install && npm install webpack && \
    npm i @vue/compiler-sfc @popperjs/core && \
    npm run production && \
    npm cache clean --force && \
    mkdir -p ../app/static/ && \
    mv ./dist/* ../app/static/ && \
    mv ../app/escriptorium/static/* ../app/static/ && \
    chown escriptorium:escriptorium /home/escriptorium/escriptorium/ -R

# Add supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY django-init.sh /django-init.sh

# Expose Django and Flower ports
EXPOSE 8000 5555

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN apt-get install -y nginx

# /etc/nginx/conf.d/main.conf
#RUN rm /etc/nginx/conf.d/default.conf
COPY nginx.conf /etc/nginx/sites-available/default


# Default command
CMD ["docker-entrypoint.sh"]

