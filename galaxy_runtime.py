"""Shared runtime state and API transport for the Galaxy single-user container."""

import hashlib
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

APP_DIR = Path("/home/escriptorium/escriptorium/app")
MEDIA_DIR = APP_DIR / "media"
STATE_DIR = Path(os.getenv("SESSION_STATE_DIR", "/run/escriptorium"))
STARTUP = STATE_DIR / "startup.json"
SHUTDOWN = STATE_DIR / "shutdown.json"
STOP_REQUEST = STATE_DIR / "stop-request"
DATABASE_READY = STATE_DIR / "database-ready"
DATABASE_FAILED = STATE_DIR / "database-failed"
INTERNAL_TOKEN = STATE_DIR / "internal-token"
WRITE_LOCK = STATE_DIR / "writes.lock"
RECEIPTS = MEDIA_DIR / ".galaxy-imports.json"
BOOTSTRAP = STATE_DIR / "bootstrap.json"


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return {} if default is None else default


def atomic_json(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="." + path.name)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchmod(output.fileno(), mode)
            json.dump(data, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def status(path, phase, message, **fields):
    atomic_json(
        path, {"phase": phase, "message": message, "updated_at": time.time(), **fields}
    )


def bootstrap_user():
    return os.getenv("BOOTSTRAP_USER") or read_json(BOOTSTRAP).get("user") or "admin"


def credentials():
    return (
        os.getenv("IMPORT_USER") or bootstrap_user(),
        os.getenv("IMPORT_PASSWORD") or os.getenv("BOOTSTRAP_PASSWORD") or "admin",
    )


@contextmanager
def write_lock(exclusive=False):
    with WRITE_LOCK.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare_runtime():
    import pwd
    import secrets

    if os.geteuid() != 0:
        raise RuntimeError(
            'This multi-service image requires UID 0; configure the Galaxy destination with docker_set_user: "0"'
        )
    owner = pwd.getpwnam("escriptorium")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(STATE_DIR, owner.pw_uid, owner.pw_gid)
    os.chmod(STATE_DIR, 0o750)
    for path in (DATABASE_READY, DATABASE_FAILED, STOP_REQUEST):
        path.unlink(missing_ok=True)
    status(STARTUP, "preparing", "Starting the database and preparing your session")
    status(SHUTDOWN, "running", "Session is running")
    INTERNAL_TOKEN.write_text(secrets.token_hex(32))
    WRITE_LOCK.touch()
    for path in (INTERNAL_TOKEN, WRITE_LOCK):
        os.chown(path, owner.pw_uid, owner.pw_gid)
        os.chmod(path, 0o600)


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_name(value):
    return re.sub(r"[^\w. -]", "_", value).strip(" .")[:120] or "export"


def remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError(
            "Operation timed out; the session remains available for retry"
        )
    return seconds


class API:
    def __init__(self, base_url=None):
        self.base_url = (
            base_url or os.getenv("API_URL") or "http://localhost:8000"
        ).rstrip("/")
        self.session = requests.Session()
        self.session.headers["Connection"] = "close"
        self.session.mount(
            "http://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    connect=3,
                    read=3,
                    status=3,
                    backoff_factor=0.2,
                    allowed_methods={"GET", "HEAD", "OPTIONS"},
                    status_forcelist=[502, 503, 504],
                )
            ),
        )
        if INTERNAL_TOKEN.exists():
            self.session.headers["X-Galaxy-Internal"] = (
                INTERNAL_TOKEN.read_text().strip()
            )

    def url(self, path):
        url = urljoin(self.base_url + "/", path)
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise ValueError("Refusing to forward API credentials to another host")
        return url

    def request(self, method, path, *, deadline=None, **kwargs):
        timeout = min(60, remaining(deadline)) if deadline else 60
        response = self.session.request(
            method, self.url(path), timeout=(min(10, timeout), timeout), **kwargs
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise requests.HTTPError(
                f"{method} {urlsplit(response.url).path}: HTTP {response.status_code}: {response.text[:800]}",
                response=response,
            ) from exc
        return response

    def list(self, path, *, deadline=None):
        result = []
        seen = set()
        while path:
            if path in seen:
                raise ValueError("API returned a pagination cycle")
            seen.add(path)
            data = self.request("GET", path, deadline=deadline).json()
            if isinstance(data, list):
                return result + data
            result.extend(data["results"])
            path = data.get("next")
        return result

    def connect(self, deadline):
        while True:
            remaining(deadline)
            if DATABASE_FAILED.exists():
                raise RuntimeError(DATABASE_FAILED.read_text())
            try:
                self.request("GET", "/health/ready", deadline=deadline)
                break
            except requests.RequestException:
                time.sleep(min(1, remaining(deadline)))
        user, password = credentials()
        # The health endpoint is a normal Django page: auto-login can set a
        # browser session cookie there. API clients must use token auth alone.
        self.session.cookies.clear()
        response = self.request(
            "POST",
            "/api/token-auth/",
            json={"username": user, "password": password},
            deadline=deadline,
        )
        self.session.headers["Authorization"] = "Token " + response.json()["token"]

    def tasks(self, deadline):
        return self.list("/api/tasks/", deadline=deadline)

    def wait_tasks(self, deadline, ids=None):
        while True:
            tasks = self.tasks(deadline)
            if ids is not None:
                tasks = [task for task in tasks if task["pk"] in ids]
                if len(tasks) != len(ids):
                    raise RuntimeError("An expected task report is missing")
                failed = [task for task in tasks if task["workflow_state"] in (2, 4)]
                if failed:
                    raise RuntimeError(f"Tasks failed or were cancelled: {failed}")
            if not any(task["workflow_state"] in (0, 1) for task in tasks):
                return tasks
            time.sleep(min(1, remaining(deadline)))

    def start_task(self, method, path, *, deadline, **kwargs):
        before = {task["pk"] for task in self.tasks(deadline)}
        self.request("POST", path, deadline=deadline, **kwargs)
        ids = {
            task["pk"]
            for task in self.tasks(deadline)
            if task["pk"] not in before and (task.get("method") or "").endswith(method)
        }
        if not ids:
            raise RuntimeError(
                f"The server accepted {method} but did not register its task"
            )
        return ids

    def download(self, path, destination, *, deadline, expected_size=None):
        """Only expose complete files to Galaxy's dataset discovery."""
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Hidden files are excluded explicitly by the wrapper's discovery patterns.
        fd, temporary = tempfile.mkstemp(dir=destination.parent, prefix=".partial-")
        size = 0
        try:
            with os.fdopen(fd, "wb") as output:
                os.fchmod(output.fileno(), 0o644)
                with self.request(
                    "GET", path, deadline=deadline, stream=True
                ) as response:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        remaining(deadline)
                        output.write(chunk)
                        size += len(chunk)
                if expected_size is not None and size != expected_size:
                    raise RuntimeError(
                        f"Incomplete download: expected {expected_size} bytes, received {size}"
                    )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
