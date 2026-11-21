#!/usr/bin/env python3
"""Save completed exports; drain, verify and archive a session before stopping."""

import os
from email.message import Message
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
import zipfile

import requests

from galaxy_runtime import (
    API,
    MEDIA_DIR,
    SHUTDOWN,
    STARTUP,
    STOP_REQUEST,
    atomic_json,
    digest,
    read_json,
    remaining,
    safe_name,
    status,
    write_lock,
)
from session_archive import create as create_archive

EXPORT_DIR = Path(os.getenv("EXPORT_DIR", "/data/export"))
PROJECT_DIR = Path(os.getenv("PROJECT_DIR", "/data/project"))
MODEL_EXPORT_DIR = Path(os.getenv("MODEL_EXPORT_DIR", "/data/model-export"))
STATE_FILE = MEDIA_DIR / ".galaxy-downloads.json"
shutdown_requested = False


def log(message):
    print("[export-watcher]", message, flush=True)


def request_shutdown(signum, frame):
    global shutdown_requested
    shutdown_requested = True


def extension(item, api=None, deadline=None):
    label = item.get("label") or ""
    for suffix in (".tar.gz", ".zip", ".json", ".xml", ".txt"):
        if label.lower().endswith(suffix):
            return suffix
    known = {
        "application/zip": ".zip",
        "application/gzip": ".tar.gz",
        "application/json": ".json",
        "application/xml": ".xml",
        "text/xml": ".xml",
        "text/plain": ".txt",
    }.get(item.get("mime_type"))
    if known:
        return known
    if api is not None:
        response = api.request("HEAD", item["file_url"], deadline=deadline)
        header = Message()
        header["Content-Disposition"] = response.headers.get("Content-Disposition", "")
        filename = header.get_filename() or ""
        for suffix in (".tar.gz", ".zip", ".json", ".xml", ".txt"):
            if filename.lower().endswith(suffix):
                return suffix
    return ".data"


def process_downloads(api, state, deadline, overrides=None):
    saved = set()
    for item in api.list("/api/downloads/", deadline=deadline):
        fingerprint = item["fingerprint"]
        previous = state.get(fingerprint)
        if previous:
            path = EXPORT_DIR / previous["filename"]
            if (
                path.is_file()
                and path.stat().st_size == previous["size"]
                and digest(path) == previous["sha256"]
            ):
                saved.add(fingerprint)
                continue
        if item.get("is_expired"):
            continue
        suffix = extension(item, api, deadline)
        label = (
            (overrides or {}).get(item.get("task_report_id"))
            or item.get("label")
            or "export"
        )
        filename = safe_name(label) + "-" + fingerprint[:12] + suffix
        destination = EXPORT_DIR / filename
        api.download(
            item.get("file_url") or f"/api/downloads/{fingerprint}/file/",
            destination,
            deadline=deadline,
            expected_size=item.get("file_size"),
        )
        if suffix == ".zip":
            with zipfile.ZipFile(destination) as archive:
                if archive.testzip() is not None:
                    destination.unlink()
                    raise ValueError(f"Corrupt export archive: {filename}")
        state[fingerprint] = {
            "filename": filename,
            "size": destination.stat().st_size,
            "sha256": digest(destination),
        }
        atomic_json(STATE_FILE, state)
        if os.getenv("EXPORT_DELETE_AFTER", "").lower() in ("true", "yes", "1"):
            api.request("DELETE", f"/api/downloads/{fingerprint}/", deadline=deadline)
        saved.add(fingerprint)
        log(f"Saved {filename}")
    return saved


def export_document_layers(api, state, deadline):
    if os.getenv("STOP_EXPORT", "true").lower() in ("false", "0", "no"):
        return
    file_format = os.getenv("STOP_EXPORT_FORMAT", "alto")
    for doc in api.list("/api/documents/", deadline=deadline):
        if not doc["parts_count"]:
            continue
        pk = doc["pk"]
        layers = api.list(f"/api/documents/{pk}/transcriptions/", deadline=deadline)
        detail = api.request("GET", f"/api/documents/{pk}/", deadline=deadline).json()
        regions = [block["pk"] for block in detail.get("valid_block_types") or []] + [
            "Undefined",
            "Orphan",
        ]
        # XML exporters serialize one layer. JSON can include every layer in one archive.
        for layer in layers[:1] if file_format == "json" else layers:
            status(SHUTDOWN, "exporting", f"Saving {doc['name']}: {layer['name']}")
            ids = api.start_task(
                "document_export",
                f"/api/documents/{pk}/export/",
                deadline=deadline,
                data={
                    "file_format": file_format,
                    "transcription": layer["pk"],
                    "all_transcriptions": file_format == "json",
                    "include_images": True,
                    "include_metadata": True,
                    "include_annotations": True,
                    "include_models": True,
                    "include_characters": True,
                    "region_types": regions,
                },
            )
            api.wait_tasks(deadline, ids)
            label = f"{doc['name']}-document-{pk}-{layer['name']}-layer-{layer['pk']}"
            while True:
                downloads = api.list("/api/downloads/", deadline=deadline)
                expected = {
                    item["fingerprint"]
                    for item in downloads
                    if item.get("task_report_id") in ids
                }
                saved = process_downloads(
                    api, state, deadline, {task: label for task in ids}
                )
                if expected and expected <= saved:
                    break
                # Upstream marks the task done just before registering its Download.
                time.sleep(min(1, remaining(deadline)))
            # Upstream's filenames have one-second resolution and omit the layer ID.
            # Do not let the next layer overwrite the file behind an earlier Download.
            time.sleep(min(1.05, remaining(deadline)))


def export_models(api, deadline):
    MODEL_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for model in api.list("/api/models/", deadline=deadline):
        if not model.get("file"):
            continue
        suffix = Path(urlsplit(model["file"]).path).suffix
        if suffix not in (".mlmodel", ".safetensors"):
            raise ValueError(f"Unknown model format for {model['name']}")
        destination = (
            MODEL_EXPORT_DIR / f"{safe_name(model['name'])}-model-{model['pk']}{suffix}"
        )
        api.download(
            model["file"],
            destination,
            deadline=deadline,
            expected_size=model.get("file_size") or None,
        )


def flush_and_shutdown(api, state):
    deadline = time.monotonic() + float(os.getenv("EXPORT_FLUSH_TIMEOUT", "600"))
    try:
        status(SHUTDOWN, "draining", "Finishing startup imports and running tasks")
        while read_json(STARTUP).get("phase") not in ("ready", "failed"):
            time.sleep(min(1, remaining(deadline)))
        api.wait_tasks(deadline)
        export_document_layers(api, state, deadline)
        api.wait_tasks(deadline)
        process_downloads(api, state, deadline)
        status(SHUTDOWN, "models", "Saving OCR models")
        export_models(api, deadline)
        status(SHUTDOWN, "snapshotting", "Saving the resumable session archive")
        with write_lock(exclusive=True):
            create_archive(PROJECT_DIR / "session.tar", timeout=remaining(deadline))
        status(SHUTDOWN, "complete", "All results and the session archive were saved")
        result = subprocess.run(
            [
                "supervisorctl",
                "-c",
                "/etc/supervisor/conf.d/supervisord.conf",
                "shutdown",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode:
            raise RuntimeError("Could not stop the supervisor: " + result.stderr)
        log("Verified all outputs; shutting down the container")
        return True
    except Exception as exc:
        status(SHUTDOWN, "failed", f"Saving failed: {exc}. Retry or resume editing.")
        log(f"ERROR: {exc}; keeping the session available")
        return False


def main():
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    PROJECT_DIR.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    api = API()
    api.connect(time.monotonic() + 1800)
    state = read_json(STATE_FILE)
    poll_interval = float(os.getenv("EXPORT_POLL_INTERVAL", "10"))
    checkpoint_interval = float(os.getenv("SNAPSHOT_INTERVAL", "300"))
    next_poll = 0
    next_checkpoint = time.monotonic() + checkpoint_interval
    log("Watching for exports")
    while True:
        if shutdown_requested:
            try:
                process_downloads(api, state, time.monotonic() + 7)
            except Exception as exc:
                log(f"Quick flush could not finish: {exc}")
            log("Shutdown signal received; finished-export flush ended")
            return 0
        if STOP_REQUEST.exists():
            STOP_REQUEST.unlink()
            if flush_and_shutdown(api, state):
                return 0
        now = time.monotonic()
        if now >= next_poll:
            try:
                process_downloads(api, state, now + 120)
            except (
                requests.RequestException,
                OSError,
                ValueError,
                TimeoutError,
            ) as exc:
                log(f"Export poll failed; will retry: {exc}")
            next_poll = time.monotonic() + poll_interval
        if checkpoint_interval > 0 and now >= next_checkpoint:
            try:
                if (
                    read_json(STARTUP).get("phase") == "ready"
                    and read_json(SHUTDOWN).get("phase") == "running"
                ):
                    # Queue browser writes behind this lock rather than rejecting them.
                    with write_lock(exclusive=True):
                        if not any(
                            t["workflow_state"] in (0, 1)
                            for t in api.tasks(time.monotonic() + 30)
                        ):
                            create_archive(PROJECT_DIR / "session.tar", timeout=120)
                            log("Saved periodic session checkpoint")
            except Exception as exc:
                log(f"Checkpoint failed; previous archive retained: {exc}")
            next_checkpoint = time.monotonic() + checkpoint_interval
        time.sleep(0.25)


if __name__ == "__main__":
    sys.exit(main())
