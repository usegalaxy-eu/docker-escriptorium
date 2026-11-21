"""Versioned, checked PostgreSQL + media session archives (no Django import)."""

import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import time

from galaxy_runtime import MEDIA_DIR, bootstrap_user, digest

VERSION = "26.10"
COMMIT_FILE = Path("/opt/galaxy/upstream-commit")


def database_environment():
    return {
        **os.environ,
        "PGHOST": os.getenv("SQL_HOST", "localhost"),
        "PGPORT": os.getenv("SQL_PORT", "5432"),
        "PGDATABASE": os.getenv("POSTGRES_DB", "escriptorium"),
        "PGUSER": os.getenv("POSTGRES_USER", "escriptorium"),
        "PGPASSWORD": os.getenv("POSTGRES_PASSWORD", "escriptorium"),
    }


def identity():
    version = subprocess.check_output(["pg_dump", "--version"], text=True)
    return {
        "schema_version": 1,
        "escriptorium_version": VERSION,
        "escriptorium_commit": COMMIT_FILE.read_text().strip(),
        "postgres_major": int(re.search(r"PostgreSQL\) (\d+)", version)[1]),
    }


def verify(path, *, compatible=True):
    """Reject links, traversal, duplicate members, extra/missing files and bad hashes."""
    import hashlib

    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError("Session archive contains duplicate paths")
        for member in members:
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or str(name) != member.name
            ):
                raise ValueError(f"Invalid session archive path: {member.name}")
            if member.name not in (
                "manifest.json",
                "database.dump",
            ) and not member.name.startswith("media/"):
                raise ValueError(f"Unexpected session archive member: {member.name}")
        manifest_member = archive.getmember("manifest.json")
        if manifest_member.size > 16 * 1024 * 1024:
            raise ValueError("Session manifest is too large")
        manifest = json.load(archive.extractfile(manifest_member))
        if compatible and any(
            manifest.get(key) != value for key, value in identity().items()
        ):
            raise ValueError(
                "Session archive requires the same eScriptorium commit and PostgreSQL major version"
            )
        if manifest.get("schema_version") != 1 or "database.dump" not in manifest.get(
            "files", {}
        ):
            raise ValueError("Invalid session manifest")
        if set(names) != {"manifest.json", *manifest["files"]}:
            raise ValueError("Session archive members do not match its manifest")
        for name, expected in manifest["files"].items():
            member = archive.getmember(name)
            value = hashlib.sha256()
            with archive.extractfile(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    value.update(chunk)
            if (
                member.size != expected["size"]
                or value.hexdigest() != expected["sha256"]
            ):
                raise ValueError(f"Session archive checksum failed: {name}")
        return manifest


def create(destination, *, timeout=300):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix=".session-", dir=destination.parent
    ) as temporary:
        temporary = Path(temporary)
        dump = temporary / "database.dump"
        subprocess.run(
            ["pg_dump", "--format=custom", "--file", str(dump)],
            env=database_environment(),
            check=True,
            timeout=timeout,
        )
        files = {"database.dump": dump}
        if MEDIA_DIR.exists():
            for path in sorted(MEDIA_DIR.rglob("*")):
                if path.is_symlink():
                    raise ValueError(f"Cannot archive a symlink in media: {path}")
                if path.is_file():
                    files["media/" + str(path.relative_to(MEDIA_DIR))] = path
        manifest = {
            **identity(),
            "created_at": time.time(),
            "bootstrap_user": bootstrap_user(),
            "files": {},
        }
        archive_path = temporary / "session.tar"
        with tarfile.open(archive_path, "w") as archive:
            for name, path in files.items():
                if time.monotonic() - started >= timeout:
                    raise TimeoutError("Session backup timed out")
                manifest["files"][name] = {
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                }
                archive.add(path, arcname=name, recursive=False)
            encoded = json.dumps(manifest).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(encoded)
            archive.addfile(info, io.BytesIO(encoded))
        verify(archive_path)
        with archive_path.open("rb") as archive:
            os.fsync(archive.fileno())
        os.replace(archive_path, destination)
    return manifest


def restore(source, *, timeout=300):
    manifest = verify(source)
    if os.getenv("BOOTSTRAP_USER") and os.environ["BOOTSTRAP_USER"] != manifest.get(
        "bootstrap_user", "admin"
    ):
        raise ValueError("Restore with the original BOOTSTRAP_USER or leave it unset")
    with tempfile.TemporaryDirectory(prefix="restore-session-") as temporary:
        temporary = Path(temporary)
        with tarfile.open(source, "r:") as archive:
            # All members were validated; copy regular files without applying archive modes.
            for member in archive.getmembers():
                destination = temporary / member.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with (
                    archive.extractfile(member) as data,
                    destination.open("wb") as output,
                ):
                    shutil.copyfileobj(data, output)
        subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                os.getenv("POSTGRES_DB", "escriptorium"),
                str(temporary / "database.dump"),
            ],
            env=database_environment(),
            check=True,
            timeout=timeout,
        )
        if (temporary / "media").exists():
            shutil.copytree(temporary / "media", MEDIA_DIR, dirs_exist_ok=True)
    return manifest
