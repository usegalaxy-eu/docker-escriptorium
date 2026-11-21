#!/usr/bin/env python3
"""Import read-only Galaxy datasets and models, then publish session readiness."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import pwd
import shutil
import subprocess
import tempfile
import time

from galaxy_runtime import (
    API,
    MEDIA_DIR,
    RECEIPTS,
    STARTUP,
    atomic_json,
    digest,
    read_json,
    status,
)

IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp", ".gif"}
MODELS = {".mlmodel", ".safetensors"}
DATA = IMAGES | {".pdf", ".zip", ".xml"}


def log(message):
    print("[import-data]", message, flush=True)


def input_name(name, extension):
    name = Path(name.replace("\\", "/")).name
    if name in ("", ".", ".."):
        raise ValueError("An input dataset has no usable filename")
    if Path(name).suffix.lower() not in DATA | MODELS:
        name += "." + extension.lstrip(".")
    return name


def inputs():
    manifest = os.getenv("IMPORT_MANIFEST")
    if manifest:
        data = read_json(manifest)
    else:
        data = {}
        for key, variable, default, extensions in (
            ("inputs", "IMPORT_DIR", "/data/import", DATA),
            ("models", "MODEL_DIR", "/data/models", MODELS),
        ):
            directory = Path(os.getenv(variable, default))
            data[key] = [
                {
                    "path": str(path),
                    "name": path.name,
                    "extension": path.suffix.lstrip("."),
                }
                for path in sorted(directory.glob("*"))
                if path.is_file() and path.suffix.lower() in extensions
            ]
    for key, extensions in (("inputs", DATA), ("models", MODELS)):
        for index, item in enumerate(data.get(key, [])):
            item["name"] = input_name(item["name"], item["extension"])
            if Path(item["name"]).suffix.lower() not in extensions:
                raise ValueError(f"Unsupported {key} file: {item['name']}")
            if not Path(item["path"]).is_file():
                raise ValueError(f"Input is missing or unreadable: {item['name']}")
            # Source paths may change after a restore; dataset identity does not.
            item["key"] = hashlib.sha256(
                f"{key}:{index}:{item['name']}:{digest(item['path'])}".encode()
            ).hexdigest()
    return data.get("inputs", []), data.get("models", [])


def document(api, deadline, has_data):
    explicit = os.getenv("IMPORT_DOCUMENT_ID")
    if explicit:
        return api.request(
            "GET", f"/api/documents/{int(explicit)}/", deadline=deadline
        ).json()
    # Restored sessions land in an existing document without creating empty extras.
    documents = api.list("/api/documents/", deadline=deadline)
    name = os.getenv("IMPORT_DOCUMENT_NAME", "Imported document")
    project_name = os.getenv("IMPORT_PROJECT_NAME", "Imported documents")
    if os.getenv("SESSION_ARCHIVE") and documents and not has_data:
        selected = read_json(MEDIA_DIR / ".galaxy-session.json").get("document_id")
        return next((doc for doc in documents if doc["pk"] == selected), documents[0])
    project = next(
        (
            p
            for p in api.list("/api/projects/", deadline=deadline)
            if p["name"] == project_name
        ),
        None,
    )
    if project is None:
        project = api.request(
            "POST", "/api/projects/", json={"name": project_name}, deadline=deadline
        ).json()
    found = next(
        (
            d
            for d in documents
            if d["name"] == name and d["project_id"] == project["id"]
        ),
        None,
    )
    if found:
        return found
    return api.request(
        "POST",
        "/api/documents/",
        deadline=deadline,
        json={
            "name": name,
            "project": project["slug"],
            "read_direction": os.getenv("IMPORT_READ_DIRECTION", "ltr"),
            "main_script": os.getenv("IMPORT_MAIN_SCRIPT", "Latin"),
        },
    ).json()


def image_names(items):
    """Give duplicate images unique upload names without changing their source files."""
    aliases = {}
    used = set()
    for item in items:
        if Path(item["name"]).suffix.lower() not in IMAGES:
            continue
        original = item["name"]
        stem, suffix = Path(original).stem, Path(original).suffix
        candidate, number = original, 1
        while candidate in used:
            number += 1
            candidate = f"{stem}__{number}{suffix}"
        used.add(candidate)
        item["upload_name"] = candidate
        aliases.setdefault(original, []).append(candidate)
    return aliases


def xml_content(item, aliases):
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.parse(item["path"], parser)
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if etree.QName(node).localname == "fileName":
            reference, attribute = node.text, None
        elif "imageFilename" in node.attrib:
            reference, attribute = node.attrib["imageFilename"], "imageFilename"
        else:
            continue
        reference = reference or ""
        matches = aliases.get(Path(reference.replace("\\", "/")).name, [])
        if len(matches) > 1:
            raise ValueError(
                f"XML references ambiguous image {reference!r}; use distinct image names"
            )
        if matches:
            if attribute:
                node.set(attribute, matches[0])
            else:
                node.text = matches[0]
    return io.BytesIO(etree.tostring(root, xml_declaration=True, encoding="UTF-8"))


def import_file(api, doc, item, aliases, deadline):
    name = item.get("upload_name", item["name"])
    extension = Path(name).suffix.lower()
    if extension in IMAGES:
        with open(item["path"], "rb") as source:
            api.request(
                "POST",
                f"/api/documents/{doc}/parts/",
                deadline=deadline,
                files={"image": (name, source)},
                data={"name": Path(name).stem},
            )
        return
    data = {"mode": "pdf" if extension == ".pdf" else "xml"}
    if os.getenv("IMPORT_TRANSCRIPTION_NAME"):
        data["name"] = os.environ["IMPORT_TRANSCRIPTION_NAME"]
    source = (
        xml_content(item, aliases) if extension == ".xml" else open(item["path"], "rb")
    )
    with source:
        ids = api.start_task(
            "document_import",
            f"/api/documents/{doc}/import/",
            deadline=deadline,
            data=data,
            files={"upload_file": (name, source)},
        )
    tasks = api.wait_tasks(deadline, ids)
    for task in tasks:
        message = task.get("messages") or ""
        if any(
            text in message.lower()
            for text in ("no match found", "parse error", "skipping")
        ):
            raise RuntimeError(f"Incomplete import of {name}: {message}")


def import_model(api, item, deadline):
    forced = (item.get("job") or os.getenv("IMPORT_MODEL_JOB") or "").lower()
    if forced in ("segment", "segmentation", "1"):
        job = "Segment"
    elif forced in ("recognize", "recognition", "2"):
        job = "Recognize"
    else:
        # Galaxy inputs commonly end in .dat. Keep the real model extension,
        # and load uploaded models with the same unprivileged UID as Celery.
        with tempfile.TemporaryDirectory(prefix="detect-model-") as directory:
            path = Path(directory) / ("model" + Path(item["name"]).suffix)
            shutil.copyfile(item["path"], path)
            owner = pwd.getpwnam("escriptorium")
            for target in (Path(directory), path):
                os.chown(target, owner.pw_uid, owner.pw_gid)
            result = subprocess.run(
                [
                    "runuser",
                    "-u",
                    "escriptorium",
                    "--",
                    sys.executable,
                    __file__,
                    "--detect-model",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=120,
            )
            job = json.loads(result.stdout.strip().splitlines()[-1])
        if not job:
            raise ValueError(
                f"Cannot determine model type for {item['name']}; specify IMPORT_MODEL_JOB"
            )
    names = {model["name"] for model in api.list("/api/models/", deadline=deadline)}
    name = Path(item["name"]).stem
    if name in names:
        name += "-" + item["key"][:12]
    with open(item["path"], "rb") as source:
        model = api.request(
            "POST",
            "/api/models/",
            deadline=deadline,
            data={"name": name, "job": job},
            files={"file": (item["name"], source)},
        ).json()
    if not model.get("file"):
        raise RuntimeError(f"Model {name} has no stored file")


def main():
    try:
        status(STARTUP, "preparing", "Loading your datasets and models")
        data, models = inputs()
        deadline = time.monotonic() + float(os.getenv("IMPORT_TIMEOUT", "1800"))
        api = API()
        api.connect(deadline)
        doc = document(api, deadline, bool(data))
        receipts = read_json(RECEIPTS)
        aliases = image_names(data)

        def priority(item):
            return (
                0
                if Path(item["name"]).suffix.lower() in IMAGES
                else 2
                if item["name"].lower().endswith(".xml")
                else 1
            )

        imported = 0
        for item in sorted(data, key=priority):
            key = f"{doc['pk']}:{item['key']}"
            if key in receipts:
                continue
            status(
                STARTUP, "preparing", f"Importing {item['name']}", document_id=doc["pk"]
            )
            import_file(api, doc["pk"], item, aliases, deadline)
            receipts[key] = {"name": item["name"], "document_id": doc["pk"]}
            atomic_json(RECEIPTS, receipts)
            imported += 1
        for item in models:
            if item["key"] in receipts:
                continue
            status(
                STARTUP,
                "preparing",
                f"Loading model {item['name']}",
                document_id=doc["pk"],
            )
            import_model(api, item, deadline)
            receipts[item["key"]] = {"name": item["name"], "kind": "model"}
            atomic_json(RECEIPTS, receipts)
            imported += 1
        api.wait_tasks(deadline)
        atomic_json(MEDIA_DIR / ".galaxy-session.json", {"document_id": doc["pk"]})
        status(
            STARTUP,
            "ready",
            "Your session is ready",
            document_id=doc["pk"],
            imported=imported,
        )
        log(f"Done. {imported} file(s) imported, 0 failure(s).")
        return 0
    except Exception as exc:
        status(STARTUP, "failed", f"Import failed: {exc}")
        log(f"ERROR: {exc}")
        return 1


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
        model_type = getattr(legacy, "model_type", [])
        model_types = [model_type] if isinstance(model_type, str) else model_type
        if "segmentation" in model_types:
            return 1, "Segment"
        if "recognition" in model_types:
            return 2, "Recognize"
        raise ValueError("The model does not declare segmentation or recognition")
    except Exception as e:
        last_error = e
    log(
        f"DEBUG: model type detection failed for {path}: "
        f"{type(last_error).__name__}: {last_error}"
    )
    return None, None


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--detect-model":
        print(json.dumps(detect_model_job(sys.argv[2])[1]))
    else:
        sys.exit(main())
