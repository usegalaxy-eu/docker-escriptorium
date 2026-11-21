"""Create and inspect a nontrivial session inside the test container."""
# ruff: noqa: E402

import hashlib
import json
from pathlib import Path
import sys
import zipfile

import django

django.setup()
from core.models import (
    AnnotationTaxonomy,
    Document,
    DocumentMetadata,
    ImageAnnotation,
    LineTranscription,
    Metadata,
    OcrModel,
    Project,
    Transcription,
)
from reporting.models import Download, TaskReport
from django.conf import settings


def seed(pk):
    doc = Document.objects.get(pk=pk)
    parts = list(doc.parts.order_by("pk"))
    layer = Transcription.objects.create(document=doc, name="Edited layer")
    text = LineTranscription.objects.create(
        line=parts[0].lines.first(), transcription=layer, content="Original edited text"
    )
    text.new_version(author=doc.owner.username, source="test")
    text.content = "Revised transcription"
    text.save()
    key, _ = Metadata.objects.get_or_create(name="Archive provenance")
    DocumentMetadata.objects.create(
        document=doc, key=key, value="Keep project metadata"
    )
    taxonomy = AnnotationTaxonomy.objects.create(
        document=doc, name="Reviewer notes", marker_type=2, has_comments=True
    )
    ImageAnnotation.objects.create(
        part=parts[0],
        taxonomy=taxonomy,
        comments=["Keep this annotation"],
        coordinates=[[1, 1], [20, 1], [20, 20]],
    )
    for model in OcrModel.objects.all():
        model.new_version(author=doc.owner.username, source="test")
        model.save()
    for index in range(11):
        Document.objects.create(
            name=f"Additional document {index}",
            project=doc.project,
            owner=doc.owner,
            main_script=doc.main_script,
        )
    downloads = Path(settings.MEDIA_ROOT) / "pagination-test"
    downloads.mkdir()
    for index in range(11):
        path = downloads / f"export-{index}.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.write(parts[0].image.path, "01-page.png")
            archive.write(parts[1].image.path, "page2.png")
            archive.writestr("text.xml", "<text>Hello world second line zipped</text>")
        Download.objects.create(
            user=doc.owner,
            label=f"pagination-export-{index}",
            file_path=str(path),
            file_size=path.stat().st_size,
            mime_type="application/zip",
        )
    # Put a pending task behind more than one response page of completed tasks.
    pending = TaskReport.objects.create(
        user=doc.owner, document=doc, label="Older running task", workflow_state=1
    )
    for index in range(12):
        TaskReport.objects.create(
            user=doc.owner,
            document=doc,
            label=f"Completed task {index}",
            workflow_state=3,
        )
    return pending.pk


def scene():
    models = []
    for model in OcrModel.objects.order_by("pk"):
        models.append(
            {
                "pk": model.pk,
                "name": model.name,
                "file": model.file.name,
                "versions": model.versions,
                "sha256": hashlib.sha256(
                    Path(model.file.path).read_bytes()
                ).hexdigest(),
            }
        )
    return {
        "projects": list(Project.objects.order_by("pk").values("pk", "name")),
        "documents": list(
            Document.objects.order_by("pk").values("pk", "name", "project_id")
        ),
        "layers": list(
            Transcription.objects.order_by("pk").values(
                "pk", "name", "document_id", "archived"
            )
        ),
        "text": list(
            LineTranscription.objects.order_by("pk").values(
                "pk", "content", "versions", "line_id", "transcription_id"
            )
        ),
        "metadata": list(
            DocumentMetadata.objects.order_by("pk").values(
                "document_id", "key__name", "value"
            )
        ),
        "annotations": list(
            ImageAnnotation.objects.order_by("pk").values(
                "pk", "comments", "coordinates", "part_id", "taxonomy__name"
            )
        ),
        "models": models,
        "images": [
            {
                "pk": part.pk,
                "filename": part.original_filename,
                "sha256": hashlib.sha256(
                    Path(part.image.path).read_bytes()
                ).hexdigest(),
            }
            for doc in Document.objects.order_by("pk")
            for part in doc.parts.order_by("pk")
        ],
    }


if __name__ == "__main__":
    if sys.argv[1] == "seed":
        print(
            json.dumps(
                {"pending_task": seed(int(sys.argv[2])), "scene": scene()}, default=str
            )
        )
    elif sys.argv[1] == "complete":
        TaskReport.objects.filter(pk=int(sys.argv[2])).update(workflow_state=3)
        print("{}")
    else:
        print(json.dumps(scene(), default=str))
