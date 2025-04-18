# Start from slim Python image
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential libpq-dev libxml2-dev libxslt-dev zlib1g-dev \
    libffi-dev libssl-dev git curl wget \
    redis postgresql postgresql-contrib \
    libleptonica-dev libvips nano default-jre default-jdk ant \
    tesseract-ocr supervisor \
    && apt-get clean

RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - && \
    apt-get install -y nodejs

# Set JAVA_HOME
RUN export JAVA_HOME=$(dirname $(dirname $(readlink -f  /usr/bin/java)))

# Add non privileged user to run the app
RUN addgroup --system uwsgi
RUN adduser --system --no-create-home --ingroup uwsgi uwsgi


# Create app user and working dir
RUN useradd -ms /bin/bash escriptorium
WORKDIR /home/escriptorium


# Now clone the full app repo
#RUN git clone https://github.com/UB-Mannheim/escriptorium.git && \

RUN git clone https://gitlab.com/scripta/escriptorium.git && \
    cd escriptorium && \
    ##git checkout fix-build-errors && \
    chown escriptorium:escriptorium ../escriptorium -R


# Install Python deps
RUN pip install --upgrade pip && pip install -r ./escriptorium/app/requirements.txt
RUN pip install kraken flower gunicorn

#ADD webpack.common.js /home/escriptorium/escriptorium/front/webpack.common.js
#RUN sed -i '1 s/^.*$/window.Vue = require("vue");/' /home/escriptorium/escriptorium/front/src/editor/mixins.js 
RUN cd /home/escriptorium/escriptorium/front && npm install
RUN cd /home/escriptorium/escriptorium/front && npm i @vue/compiler-sfc @popperjs/core && npm run production



RUN cd /home/escriptorium/escriptorium/front && mkdir -p ../app/static/ && cp ./dist/* ../app/static/ -R && \
    mkdir -p ../app/static/ && \
    cp ../app/escriptorium/static/* ../app/static/ -r && \
    chown escriptorium:escriptorium /home/escriptorium/escriptorium/ -R


# Add supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY django-init.sh /django-init.sh

# Expose Django and Flower ports
EXPOSE 8000 5555

ENV DB_NAME=escriptorium \
    DB_USER=escriptorium \
    DB_PASSWORD=escriptorium \
    DB_PORT=5432

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Default command
CMD ["docker-entrypoint.sh"]

