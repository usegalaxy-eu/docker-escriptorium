#!/bin/bash

export POSTGRES_DB=escriptorium
export POSTGRES_USER=escriptorium
export POSTGRES_PASSWORD=escriptorium

su - postgres -c "psql -c \"CREATE USER escriptorium WITH PASSWORD 'escriptorium';\""
su - postgres -c "psql -c \"CREATE DATABASE escriptorium OWNER escriptorium;\""

su escriptorium -p -c "cd /home/escriptorium/escriptorium/app/ && python manage.py migrate && python manage.py collectstatic --noinput"

