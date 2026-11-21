#!/usr/bin/env python3
"""
export-watcher.py — materialize eScriptorium exports into a folder that
Galaxy can pick up.

eScriptorium produces exports (PAGE XML, ALTO, zip archives, ...) through
asynchronous celery tasks. Once an export is finished it is registered in
the Downloads API:

    GET  /api/downloads/                     list the user's downloads
    GET  /api/downloads/<fingerprint>/file/  stream the archive

This script polls that API and saves every new download into EXPORT_DIR
(default /data/export). It is intended for Galaxy Interactive Tool (GxIT)
integration:

  * the GxIT tool XML bind-mounts the Galaxy job working directory and
    points EXPORT_DIR at a subdirectory of it,
  * the tool declares  <discover_datasets pattern="__name_and_ext__"
    directory="exports"/>  on that subdirectory,
  * when the user ends the interactive session, Galaxy harvests every
    exported archive into the history as a dataset collection.

The watcher keeps its state in /tmp/export-watcher-state.json so the
EXPORT_DIR itself stays clean (only user-visible files) and restarts do
not re-download everything. Already-seen exports are tracked by fingerprint.

Environment variables (all optional):
  API_URL               base URL of eScriptorium (default http://localhost:8000)
  EXPORT_DIR            directory to save exports into (default /data/export)
  IMPORT_USER           API username (default: bootstrap superuser, "admin")
  IMPORT_PASSWORD       API password (default: bootstrap password, "admin")
  EXPORT_POLL_INTERVAL  seconds between polls (default 10)
  EXPORT_DELETE_AFTER   if truthy, delete the download from eScriptorium
                        after saving it (default: keep it)
  EXPORT_FLUSH_TIMEOUT  max seconds to wait for running tasks/exports when a
                        shutdown was requested (default 300)
  STOP_EXPORT           if "no", skip the automatic full-document export on
                        shutdown (default: export every document)
  STOP_EXPORT_FORMAT    file format of the automatic export ("alto", "pagexml"
                        or "json", default "alto")

Shutdown (Galaxy "Stop session" button / galaxy_integration.py): when the
file /tmp/export-watcher-shutdown appears, the watcher triggers a full
export of every document (transcriptions + images, a re-importable backup),
waits for all running tasks to finish, saves every pending export into
EXPORT_DIR and then shuts the whole container down gracefully
(supervisorctl shutdown), so Galaxy can harvest the results.

Closing the session from the Galaxy side (docker stop → SIGTERM): the
watcher catches the signal and performs a *quick flush* — it saves every
export that has already finished before exiting. Exports still running at
that moment cannot be completed within the docker stop grace period; users
who want a guaranteed full backup (including an automatic final document
export) should use the "Stop session and export to Galaxy" button instead.
"""

import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import time

import requests

API_URL = (os.getenv("API_URL") or "http://localhost:8000").rstrip("/")
EXPORT_DIR = os.getenv("EXPORT_DIR") or "/data/export"
IMPORT_USER = os.getenv("IMPORT_USER") or "admin"
IMPORT_PASSWORD = os.getenv("IMPORT_PASSWORD") or "admin"
POLL_INTERVAL = int(os.getenv("EXPORT_POLL_INTERVAL") or 10)
DELETE_AFTER = os.getenv("EXPORT_DELETE_AFTER", "False").lower() in ("true", "1", "yes")
FLUSH_TIMEOUT = int(os.getenv("EXPORT_FLUSH_TIMEOUT") or 300)
STOP_EXPORT = os.getenv("STOP_EXPORT", "True").lower() not in ("false", "0", "no", "none")
STOP_EXPORT_FORMAT = os.getenv("STOP_EXPORT_FORMAT") or "alto"

STATE_FILE = "/tmp/export-watcher-state.json"
SHUTDOWN_FLAG = "/tmp/export-watcher-shutdown"

# TaskReport.workflow_state values that mean "still working on it"
PENDING_TASK_STATES = (0, 1)  # queued, started

MIME_EXTENSIONS = {
    "application/zip": ".zip",
    "application/gzip": ".tar.gz",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "application/octet-stream": ".bin",
}


def log(*args):
    print("[export-watcher]", *args, flush=True)


# Set by the SIGTERM/SIGINT handler: docker stop / Galaxy closing the session.
shutdown_requested = False


def request_shutdown(signum, frame):
    global shutdown_requested
    shutdown_requested = True


def quick_flush(session, state, budget=7):
    """Save exports that have already finished, without waiting for running
    tasks (there is no time for that during a container stop — docker's
    default grace period is 10s). Exports still running at that point cannot
    be saved; the "Stop session and export to Galaxy" button exists for a full flush."""
    deadline = time.time() + budget
    saved_total = 0
    while time.time() < deadline:
        try:
            saved_total += process_downloads(session, state)
        except requests.RequestException as e:
            log(f"WARNING: quick flush failed ({e.__class__.__name__}: {e})")
            break
        time.sleep(1)
    return saved_total


def wait_for_api(minutes=60):
    """Wait until /health/ready reports the backing services as up."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
        if shutdown_requested:
            return False
        try:
            r = requests.get(f"{API_URL}/health/ready", timeout=10)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
    return False


def get_token(session, minutes=30):
    """Fetch an API token, retrying while migrations may still be running."""
    deadline = time.time() + minutes * 60
    last_error = None
    while time.time() < deadline:
        if shutdown_requested:
            return None
        try:
            r = session.post(
                f"{API_URL}/api/token-auth/",
                json={"username": IMPORT_USER, "password": IMPORT_PASSWORD},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["token"]
        except (requests.RequestException, ValueError, KeyError) as e:
            last_error = e
            time.sleep(10)
    raise RuntimeError(f"could not obtain API token: {last_error}")


def load_state():
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_FILE)


def sanitize(name):
    name = re.sub(r"[^\w\-. ]", "_", name).strip()
    return name[:150] or "export"


def extension_and_base(item):
    """Return (extension, base name): use the label's own extension when it
    is a known export type, otherwise derive the extension from the mime type."""
    label = item.get("label") or ""
    root, ext = os.path.splitext(label)
    if label.endswith(".tar.gz"):
        return ".tar.gz", label[:-7]
    if ext.lower() in (".zip", ".tgz", ".json", ".xml", ".alto", ".html"):
        return ext.lower(), root
    return MIME_EXTENSIONS.get((item.get("mime_type") or "").lower(), ".bin"), label


def unique_path(directory, base, ext):
    candidate = os.path.join(directory, base + ext)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base}_{counter}{ext}")
        counter += 1
    return candidate


def process_downloads(session, state):
    r = session.get(f"{API_URL}/api/downloads/?page_size=100", timeout=60)
    r.raise_for_status()
    items = r.json().get("results", r.json())

    saved = 0
    for item in items:
        fingerprint = item.get("fingerprint")
        if not fingerprint or fingerprint in state:
            continue

        ext, base = extension_and_base(item)
        filename = unique_path(EXPORT_DIR, sanitize(base), ext)

        url = item.get("file_url") or f"/api/downloads/{fingerprint}/file/"
        if url.startswith("/"):
            url = API_URL + url
        log(f"Saving export '{item.get('label')}' -> {filename}")
        try:
            with session.get(url, stream=True, timeout=3600) as resp:
                resp.raise_for_status()
                with open(filename, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        fh.write(chunk)
        except requests.RequestException as e:
            log(f"ERROR downloading {fingerprint}: {e}")
            continue

        state[fingerprint] = os.path.basename(filename)
        saved += 1

        if DELETE_AFTER:
            try:
                session.delete(f"{API_URL}/api/downloads/{fingerprint}/", timeout=30)
                log(f"Deleted download {fingerprint} from eScriptorium")
            except requests.RequestException as e:
                log(f"WARNING: could not delete download {fingerprint}: {e}")

    if saved:
        save_state(state)
    return saved


def pending_tasks(session):
    """Return the user's task reports that are queued or running."""
    r = session.get(f"{API_URL}/api/tasks/?page_size=100", timeout=60)
    r.raise_for_status()
    items = r.json().get("results", r.json())
    return [t for t in items if t.get("workflow_state") in PENDING_TASK_STATES]


def document_transcriptions(session, doc_pk):
    """Return the document's (non-archived) transcriptions as a list."""
    r = session.get(f"{API_URL}/api/documents/{doc_pk}/transcriptions/", timeout=60)
    r.raise_for_status()
    body = r.json()
    return body.get("results", body) if isinstance(body, dict) else body


def trigger_document_exports(session):
    """Ask eScriptorium to export every document the user can access as a
    full archive (all transcriptions + images + metadata). This is what the
    Galaxy "Stop session and export to Galaxy" button relies on: the user gets a
    re-importable backup of everything they worked on, without having to
    click Export per document. The resulting downloads are saved into
    EXPORT_DIR by the normal flush loop.

    Configure with STOP_EXPORT_FORMAT (file format, default "alto") and
    STOP_EXPORT (set to "no" to disable the automatic export).
    """
    if not STOP_EXPORT:
        return 0

    r = session.get(f"{API_URL}/api/documents/?page_size=100", timeout=60)
    r.raise_for_status()
    documents = r.json().get("results", r.json())

    triggered = 0
    for doc in documents:
        pk = doc.get("pk")
        name = doc.get("name") or f"document {pk}"
        try:
            transcriptions = document_transcriptions(session, pk)
            if not transcriptions:
                log(f"Skipping export of '{name}': no transcription yet")
                continue
            # region_types is required by the export form; the UI pre-selects
            # all of the document's block types plus the Undefined/Orphan
            # special choices — do the same.
            detail = session.get(f"{API_URL}/api/documents/{pk}/", timeout=60)
            detail.raise_for_status()
            region_types = [
                bt["pk"] for bt in detail.json().get("valid_block_types") or []
            ] + ["Undefined", "Orphan"]
            payload = {
                "file_format": STOP_EXPORT_FORMAT,
                "transcription": transcriptions[0]["pk"],
                "all_transcriptions": True,
                "include_images": True,
                "include_metadata": True,
                "region_types": region_types,
            }
            r = session.post(
                f"{API_URL}/api/documents/{pk}/export/", data=payload, timeout=60
            )
            if r.status_code == 200:
                log(f"Triggered export of document '{name}' ({STOP_EXPORT_FORMAT} archive)")
                triggered += 1
            else:
                log(f"ERROR: export of '{name}' failed: HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            log(f"ERROR: could not export '{name}': {e}")
    return triggered


def flush_and_shutdown(session, state):
    """Wait for running tasks, save all pending exports, stop the container."""
    log("Shutdown requested — waiting for running tasks and flushing exports")
    try:
        triggered = trigger_document_exports(session)
        if triggered:
            log(f"Waiting for {triggered} document export(s) to finish…")
    except requests.RequestException as e:
        log(f"WARNING: could not trigger document exports ({e.__class__.__name__}: {e})")
    deadline = time.time() + FLUSH_TIMEOUT
    while time.time() < deadline:
        try:
            pending = pending_tasks(session)
        except requests.RequestException as e:
            log(f"WARNING: could not list tasks ({e.__class__.__name__}), assuming none")
            pending = []
        try:
            saved = process_downloads(session, state)
        except requests.RequestException as e:
            log(f"WARNING: final poll failed ({e.__class__.__name__})")
            saved = 0
        if not pending and not saved:
            break
        log(f"Waiting: {len(pending)} task(s) running, {saved} export(s) saved this round")
        time.sleep(5)
    else:
        log(f"WARNING: flush timeout ({FLUSH_TIMEOUT}s) reached, shutting down anyway")

    try:
        os.remove(SHUTDOWN_FLAG)
    except OSError:
        pass

    log("Flush finished — shutting down the container")
    try:
        subprocess.run(
            ["supervisorctl", "-c", "/etc/supervisor/conf.d/supervisord.conf", "shutdown"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError as e:
        log(f"supervisorctl failed ({e})")
    # Fallback if the supervisor socket is unavailable: SIGTERM to supervisord
    # (PID 1 in the container) triggers the same graceful shutdown.
    time.sleep(10)
    try:
        os.kill(1, signal.SIGTERM)
    except OSError:
        pass


def main():
    os.makedirs(EXPORT_DIR, exist_ok=True)

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)

    if not wait_for_api():
        if shutdown_requested:
            log("Shutdown signal received during startup — exiting.")
            return 0
        log("ERROR: API did not become ready in time, aborting.")
        return 1

    session = requests.Session()
    token = get_token(session)
    if token is None:
        log("Shutdown signal received during startup — exiting.")
        return 0
    session.headers["Authorization"] = f"Token {token}"

    state = load_state()
    log(f"Watching for exports, saving to {EXPORT_DIR} (poll every {POLL_INTERVAL}s)")

    while True:
        if shutdown_requested:
            saved = quick_flush(session, state)
            log(f"Shutdown signal received — flushed {saved} finished export(s), exiting")
            return 0
        if os.path.exists(SHUTDOWN_FLAG):
            flush_and_shutdown(session, state)
            return 0
        try:
            saved = process_downloads(session, state)
            if saved:
                log(f"Saved {saved} export(s)")
        except requests.RequestException as e:
            log(f"WARNING: poll failed ({e.__class__.__name__}: {e}), retrying")
        # sleep in short slices so a SIGTERM is noticed immediately
        for _ in range(POLL_INTERVAL):
            if shutdown_requested or os.path.exists(SHUTDOWN_FLAG):
                break
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
