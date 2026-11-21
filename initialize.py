#!/usr/bin/env python3
"""Initialize the database once; restore before starting Django or Celery."""

import json
import os
import pwd
import subprocess
import sys
import time

from galaxy_runtime import (
    APP_DIR,
    BOOTSTRAP,
    DATABASE_FAILED,
    DATABASE_READY,
    MEDIA_DIR,
    STARTUP,
    STATE_DIR,
    digest,
    atomic_json,
    status,
)


def database():
    import psycopg2
    from psycopg2 import sql

    connection = psycopg2.connect(
        dbname="postgres", user="postgres", host="/var/run/postgresql"
    )
    connection.autocommit = True
    with connection.cursor() as cursor:
        name = os.getenv("POSTGRES_DB", "escriptorium")
        user = os.getenv("POSTGRES_USER", "escriptorium")
        password = os.getenv("POSTGRES_PASSWORD", "escriptorium")
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (user,))
        if cursor.fetchone():
            cursor.execute(
                sql.SQL("ALTER ROLE {} PASSWORD %s").format(sql.Identifier(user)),
                (password,),
            )
        else:
            cursor.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(
                    sql.Identifier(user)
                ),
                (password,),
            )
        cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,))
        fresh = cursor.fetchone() is None
        if fresh:
            cursor.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier(user)
                )
            )
    print(json.dumps({"fresh": fresh}))


def application(fresh):
    os.chdir(APP_DIR)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "escriptorium.settings")
    # Upstream migration 0004 uses these names when it seeds the first account.
    for target, source, default in (
        ("DJANGO_SU_NAME", "BOOTSTRAP_USER", "admin"),
        ("DJANGO_SU_EMAIL", "BOOTSTRAP_EMAIL", "admin@example.com"),
        ("DJANGO_SU_PASSWORD", "BOOTSTRAP_PASSWORD", "admin"),
    ):
        os.environ[target] = os.getenv(source) or default
    import django

    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.management import call_command

    call_command("migrate", interactive=False)
    user, created = get_user_model().objects.get_or_create(
        username=os.environ["DJANGO_SU_NAME"]
    )
    user.is_staff = user.is_superuser = user.is_active = True
    user.email = os.environ["DJANGO_SU_EMAIL"]
    if not user.check_password(os.environ["DJANGO_SU_PASSWORD"]):
        user.set_password(os.environ["DJANGO_SU_PASSWORD"])
    if fresh or created:
        user.legacy_mode = False
    user.save()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "database":
        database()
        return
    if len(sys.argv) > 1 and sys.argv[1] == "application":
        application(sys.argv[2] == "fresh")
        return
    try:
        print("Database initialization started.", flush=True)
        deadline = time.monotonic() + 120
        while subprocess.run(["pg_isready", "-q", "-h", "localhost"]).returncode:
            if time.monotonic() >= deadline:
                raise TimeoutError("PostgreSQL did not start")
            time.sleep(1)
        output = subprocess.check_output(
            ["runuser", "-u", "postgres", "--", sys.executable, __file__, "database"],
            text=True,
        )
        fresh = json.loads(output)["fresh"]
        archive = os.getenv("SESSION_ARCHIVE")
        restored = STATE_DIR / "restored-archive"
        if archive:
            if fresh:
                from session_archive import restore

                manifest = restore(
                    archive, timeout=int(os.getenv("RESTORE_TIMEOUT", "600"))
                )
                os.environ.setdefault(
                    "BOOTSTRAP_USER", manifest.get("bootstrap_user", "admin")
                )
                restored.write_text(digest(archive))
            elif not restored.exists() or restored.read_text() != digest(archive):
                raise RuntimeError("Restoring a session requires a fresh database")
        if archive and BOOTSTRAP.exists():
            os.environ.setdefault(
                "BOOTSTRAP_USER", json.loads(BOOTSTRAP.read_text())["user"]
            )
        atomic_json(BOOTSTRAP, {"user": os.getenv("BOOTSTRAP_USER", "admin")})
        owner = pwd.getpwnam("escriptorium")
        if MEDIA_DIR.exists():
            for path in [MEDIA_DIR, *MEDIA_DIR.rglob("*")]:
                os.chown(path, owner.pw_uid, owner.pw_gid, follow_symlinks=False)
        subprocess.run(
            [
                "runuser",
                "-u",
                "escriptorium",
                "--",
                sys.executable,
                __file__,
                "application",
                "fresh" if fresh and not archive else "existing",
            ],
            check=True,
        )
        DATABASE_READY.touch()
        print("Database initialization finished.", flush=True)
    except Exception as exc:
        DATABASE_FAILED.write_text(str(exc))
        status(STARTUP, "failed", f"Database initialization failed: {exc}")
        raise


if __name__ == "__main__":
    main()
