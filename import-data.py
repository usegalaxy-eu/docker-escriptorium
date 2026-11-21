#!/usr/bin/env python3
"""
import-data.py — load files into eScriptorium at container startup.

Runs as a one-shot supervisor program once the web server is up. It logs in
with the bootstrap superuser via the REST API and imports every file found in
IMPORT_DIR, so the data is already loaded when a user first visits the site.

Supported files (matched by extension, processed in alphabetical order):

  *.png *.jpg *.jpeg *.tif *.tiff *.webp *.bmp *.gif   -> page images
      uploaded to the target document (POST /api/documents/<id>/parts/)

  *.pdf      -> imported as pages (mode=pdf, images are extracted)
  *.xml      -> ALTO / PAGE / Abbyy XML segmentation & transcription
                (mode=xml)
  *.zip      -> zip archive of images and/or XML files; this also covers
                eScriptorium document export archives, which are plain
                zips of images plus PAGE XML (mode=xml)

Kraken models (*.mlmodel, *.safetensors) found in MODEL_DIR are uploaded as
OCR models (POST /api/models/). Whether a model is a segmentation or a
recognition model is detected by loading it with the kraken library (same
detection eScriptorium's upload form uses); override with IMPORT_MODEL_JOB.

Successfully imported files are moved to <IMPORT_DIR>/.imported (models to
<MODEL_DIR>/.imported) so a container restart does not import them twice.

Environment variables (all optional):
  IMPORT_DIR            directory to scan (default /data/import)
  IMPORT_DIR_WAIT       seconds to wait for a late-created IMPORT_DIR
                        (default 60, e.g. Galaxy binding the job workdir)
  IMPORT_USER           API username (default: bootstrap superuser, "admin")
  IMPORT_PASSWORD       API password (default: bootstrap password, "admin")
  IMPORT_DOCUMENT_ID    import into this existing document id (optional)
  IMPORT_PROJECT_NAME   project to use/create (default "Imported documents")
  IMPORT_DOCUMENT_NAME  document to use/create (default "Imported document")
  IMPORT_READ_DIRECTION "ltr" or "rtl" for new documents (default "ltr")
  IMPORT_MAIN_SCRIPT    main script name for new documents, e.g. "Latin"
  IMPORT_TRANSCRIPTION_NAME  name for transcriptions imported from XML
  MODEL_DIR             directory with Kraken models to upload
                        (*.mlmodel/*.safetensors, default /data/models)
  MODEL_DIR_WAIT        seconds to wait for a late-created MODEL_DIR
                        (default 60)
  IMPORT_MODEL_JOB      force the model type: "segment" or "recognize"
                        (default: auto-detect with the kraken library)
  API_URL               base URL of the eScriptorium instance
                        (default http://localhost:8000)
"""

import os
import shutil
import sys
import time

import requests

API_URL = (os.getenv("API_URL") or "http://localhost:8000").rstrip("/")
IMPORT_DIR = os.getenv("IMPORT_DIR") or "/data/import"
IMPORT_USER = os.getenv("IMPORT_USER") or "admin"
IMPORT_PASSWORD = os.getenv("IMPORT_PASSWORD") or "admin"
IMPORT_DOCUMENT_ID = os.getenv("IMPORT_DOCUMENT_ID") or ""
IMPORT_PROJECT_NAME = os.getenv("IMPORT_PROJECT_NAME") or "Imported documents"
IMPORT_DOCUMENT_NAME = os.getenv("IMPORT_DOCUMENT_NAME") or "Imported document"
IMPORT_READ_DIRECTION = os.getenv("IMPORT_READ_DIRECTION") or "ltr"
IMPORT_MAIN_SCRIPT = os.getenv("IMPORT_MAIN_SCRIPT") or "Latin"
IMPORT_TRANSCRIPTION_NAME = os.getenv("IMPORT_TRANSCRIPTION_NAME") or ""

MODEL_DIR = os.getenv("MODEL_DIR") or "/data/models"
IMPORT_MODEL_JOB = os.getenv("IMPORT_MODEL_JOB") or ""

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
PDF_EXTENSIONS = {".pdf"}
XML_EXTENSIONS = {".xml"}
ARCHIVE_EXTENSIONS = {".zip"}
MODEL_EXTENSIONS = {".mlmodel", ".safetensors"}

DONE_DIR = os.path.join(IMPORT_DIR, ".imported")
MODEL_DONE_DIR = os.path.join(MODEL_DIR, ".imported")

API_TIMEOUT = 600  # large uploads (PDFs, zips) can take a while


def log(*args):
    print("[import-data]", *args, flush=True)


def wait_for_api(minutes=30):
    """Wait until /health/ready reports the backing services as up."""
    deadline = time.time() + minutes * 60
    while time.time() < deadline:
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


def find_or_create_project(session):
    r = session.get(f"{API_URL}/api/projects/?name={IMPORT_PROJECT_NAME}", timeout=30)
    r.raise_for_status()
    results = r.json().get("results", r.json())
    for project in results:
        if project["name"] == IMPORT_PROJECT_NAME:
            return project
    log(f"Creating project '{IMPORT_PROJECT_NAME}'")
    r = session.post(f"{API_URL}/api/projects/", json={"name": IMPORT_PROJECT_NAME}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_or_create_document(session, project):
    r = session.get(f"{API_URL}/api/documents/?page_size=1000", timeout=30)
    r.raise_for_status()
    results = r.json().get("results", r.json())
    for document in results:
        if document["name"] == IMPORT_DOCUMENT_NAME and document.get("project_id") == project["pk"]:
            return document

    log(f"Creating document '{IMPORT_DOCUMENT_NAME}' in project '{project['name']}'")
    payload = {
        "name": IMPORT_DOCUMENT_NAME,
        "project": project["slug"],
        "read_direction": IMPORT_READ_DIRECTION,
        # v26.10 requires a main script (from the seeded Script table);
        # override with IMPORT_MAIN_SCRIPT if "Latin" is not appropriate.
        "main_script": IMPORT_MAIN_SCRIPT,
    }
    r = session.post(f"{API_URL}/api/documents/", json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def import_file(session, document_id, path):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()

    if ext in IMAGE_EXTENSIONS:
        log(f"Uploading image {name}")
        with open(path, "rb") as fh:
            r = session.post(
                f"{API_URL}/api/documents/{document_id}/parts/",
                files={"image": (name, fh)},
                data={"name": os.path.splitext(name)[0]},
                timeout=API_TIMEOUT,
            )
    elif ext in PDF_EXTENSIONS:
        log(f"Importing PDF {name}")
        with open(path, "rb") as fh:
            r = session.post(
                f"{API_URL}/api/documents/{document_id}/import/",
                data={"mode": "pdf"},
                files={"upload_file": (name, fh)},
                timeout=API_TIMEOUT,
            )
    elif ext in XML_EXTENSIONS or ext in ARCHIVE_EXTENSIONS:
        log(f"Importing {'archive' if ext == '.zip' else 'XML'} {name}")
        data = {"mode": "xml"}
        if IMPORT_TRANSCRIPTION_NAME:
            data["name"] = IMPORT_TRANSCRIPTION_NAME
        with open(path, "rb") as fh:
            r = session.post(
                f"{API_URL}/api/documents/{document_id}/import/",
                data=data,
                files={"upload_file": (name, fh)},
                timeout=API_TIMEOUT,
            )
    else:
        log(f"Skipping unsupported file {name}")
        return None

    if r.status_code not in (200, 201):
        log(f"ERROR importing {name}: HTTP {r.status_code}: {r.text[:500]}")
        return False
    return True


def detect_model_job(path):
    """Return (job id, API choice name) for a model file: 1/"Segment" or
    2/"Recognize". Uses the same detection as eScriptorium's upload form
    (load the file with the kraken library and see which task accepts it)."""
    last_error = None
    try:
        from kraken.tasks import SegmentationTaskModel
        SegmentationTaskModel.load_model(path)
        return 1, "Segment"
    except Exception as e:
        last_error = e
    try:
        from kraken.tasks import RecognitionTaskModel
        RecognitionTaskModel.load_model(path)
        return 2, "Recognize"
    except Exception as e:
        last_error = e
    try:
        from kraken.lib import vgsl
        legacy = vgsl.TorchVGSLModel.load_model(path)
        if getattr(legacy, "model_type", "") == "segmentation":
            return 1, "Segment"
        return 2, "Recognize"
    except Exception as e:
        last_error = e
    log(f"DEBUG: model type detection failed for {path}: "
        f"{type(last_error).__name__}: {last_error}")
    return None, None


def forced_model_job():
    """(job id, API choice name) from IMPORT_MODEL_JOB, or None if unset."""
    value = IMPORT_MODEL_JOB.strip().lower()
    if value in ("segment", "segmentation", "seg", "1"):
        return (1, "Segment")
    if value in ("recognize", "recognition", "rec", "2"):
        return (2, "Recognize")
    return None


def list_dir_files(directory, extensions=None):
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, f)
        for f in os.listdir(directory)
        if os.path.isfile(os.path.join(directory, f))
        and (extensions is None or os.path.splitext(f)[1].lower() in extensions)
    )


def import_models(session):
    """Upload every Kraken model found in MODEL_DIR as an OCR model."""
    files = list_dir_files(MODEL_DIR, MODEL_EXTENSIONS)
    if not files:
        log(f"No models to import in {MODEL_DIR}.")
        return 0, 0

    # skip models that already exist (idempotent restarts)
    r = session.get(f"{API_URL}/api/models/?page_size=1000", timeout=30)
    r.raise_for_status()
    body = r.json()
    existing = {m["name"] for m in (body.get("results", body) if isinstance(body, dict) else body)}

    os.makedirs(MODEL_DONE_DIR, exist_ok=True)
    forced = forced_model_job()

    imported = failures = 0
    for path in files:
        name = os.path.basename(path)
        base = os.path.splitext(name)[0]
        if base in existing:
            log(f"Skipping model '{base}': a model with this name already exists")
            shutil.move(path, os.path.join(MODEL_DONE_DIR, name))
            continue
        if forced:
            job, job_name = forced
        else:
            job, job_name = detect_model_job(path)
        if job is None:
            log(f"WARNING: could not determine whether '{name}' is a segmentation "
                "or recognition model (IMPORT_MODEL_JOB can force it), assuming recognition")
            job, job_name = 2, "Recognize"
        log(f"Uploading model {name} (job={job_name.lower()})")
        try:
            with open(path, "rb") as fh:
                r = session.post(
                    f"{API_URL}/api/models/",
                    data={"name": base, "job": job_name},
                    files={"file": (name, fh)},
                    timeout=API_TIMEOUT,
                )
        except requests.RequestException as e:
            log(f"ERROR while uploading model {name}: {e}")
            failures += 1
            continue
        if r.status_code in (200, 201):
            shutil.move(path, os.path.join(MODEL_DONE_DIR, name))
            imported += 1
        else:
            log(f"ERROR uploading model {name}: HTTP {r.status_code}: {r.text[:500]}")
            failures += 1
    return imported, failures


def wait_for_dir(directory, wait_env, default_wait):
    """Wait for a directory that may be created/bound dynamically (e.g. a
    Galaxy interactive tool binds the job working directory just before the
    stack starts)."""
    wait = int(os.getenv(wait_env) or default_wait)
    deadline = time.time() + wait
    while not os.path.isdir(directory):
        if time.time() >= deadline:
            return False
        time.sleep(2)
    return True


def main():
    wait_for_dir(IMPORT_DIR, "IMPORT_DIR_WAIT", 60)
    wait_for_dir(MODEL_DIR, "MODEL_DIR_WAIT", 60)

    files = list_dir_files(IMPORT_DIR)
    if not files:
        log(f"No files to import in {IMPORT_DIR}.")

    if not files and not list_dir_files(MODEL_DIR, MODEL_EXTENSIONS):
        return 0

    if not wait_for_api():
        log("ERROR: API did not become ready in time, aborting.")
        return 1

    session = requests.Session()
    session.headers["Authorization"] = f"Token {get_token(session)}"

    imported = failures = 0

    if files:
        if IMPORT_DOCUMENT_ID:
            document_id = IMPORT_DOCUMENT_ID
            log(f"Using existing document #{document_id}")
        else:
            project = find_or_create_project(session)
            document = find_or_create_document(session, project)
            document_id = document["pk"]
            log(f"Using document #{document_id} ({document['name']})")

        os.makedirs(DONE_DIR, exist_ok=True)

        for path in files:
            try:
                result = import_file(session, document_id, path)
            except requests.RequestException as e:
                log(f"ERROR while importing {path}: {e}")
                failures += 1
                continue
            if result is None:
                continue  # unsupported file type, left in place
            if result:
                shutil.move(path, os.path.join(DONE_DIR, os.path.basename(path)))
                imported += 1
            else:
                failures += 1

    if list_dir_files(MODEL_DIR, MODEL_EXTENSIONS) or os.path.isdir(MODEL_DIR):
        m_imported, m_failures = import_models(session)
        imported += m_imported
        failures += m_failures

    log(f"Done. {imported} file(s) imported, {failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
