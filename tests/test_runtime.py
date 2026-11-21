"""Regression checks for the Galaxy import, export and session contracts."""

from contextlib import nullcontext
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import requests

import galaxy_runtime as runtime
import session_archive

ROOT = Path(__file__).resolve().parent.parent


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


importer = module("importer", "import-data.py")
watcher = module("watcher", "export-watcher.py")


class APITests(unittest.TestCase):
    def test_pagination_follows_next_instead_of_assuming_page_size(self):
        api = runtime.API("http://localhost:8000")
        api.request = Mock(
            side_effect=[
                Mock(json=lambda: {"results": [1], "next": "/api/tasks/?page=2"}),
                Mock(json=lambda: {"results": [2], "next": None}),
            ]
        )
        self.assertEqual(api.list("/api/tasks/"), [1, 2])

    def test_pagination_rejects_credential_forwarding_to_another_host(self):
        with self.assertRaises(ValueError):
            runtime.API("http://localhost:8000").url(
                "https://untrusted.example/api/tasks/"
            )

    def test_interrupted_download_preserves_previous_complete_file(self):
        api = runtime.API()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)

        def chunks(**kwargs):
            yield b"partial"
            raise requests.ConnectionError("transfer interrupted")

        response.iter_content = chunks
        api.request = Mock(return_value=response)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.zip"
            target.write_bytes(b"previous complete result")
            with self.assertRaises(requests.ConnectionError):
                api.download("/file", target, deadline=time.monotonic() + 10)
            self.assertEqual(target.read_bytes(), b"previous complete result")
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_download_size_mismatch_is_not_exposed(self):
        api = runtime.API()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content = lambda **kwargs: iter([b"short"])
        api.request = Mock(return_value=response)
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "result.zip"
            with self.assertRaises(RuntimeError):
                api.download(
                    "/file", target, deadline=time.monotonic() + 10, expected_size=100
                )
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])


class ImportTests(unittest.TestCase):
    def test_existing_document_matches_project_id(self):
        api = Mock()
        api.list.side_effect = [
            [{"pk": 7, "name": "Imported document", "project_id": 3}],
            [{"id": 3, "name": "Imported documents", "slug": "imported-documents"}],
        ]
        with patch.dict(
            "os.environ",
            {
                "IMPORT_DOCUMENT_NAME": "Imported document",
                "IMPORT_PROJECT_NAME": "Imported documents",
            },
        ):
            self.assertEqual(
                importer.document(api, time.monotonic() + 10, True)["pk"], 7
            )
        api.request.assert_not_called()

    def test_original_image_extension_is_not_duplicated(self):
        self.assertEqual(importer.input_name("page.png", "png"), "page.png")
        self.assertEqual(importer.input_name("page.tif", "tiff"), "page.tif")

    def test_duplicate_images_get_distinct_names(self):
        items = [{"name": "page.png"}, {"name": "page.png"}]
        importer.image_names(items)
        self.assertEqual(
            [item["upload_name"] for item in items], ["page.png", "page__2.png"]
        )

    def test_xml_with_ambiguous_image_reference_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            xml = Path(folder) / "page.xml"
            xml.write_text("<alto><fileName>page.png</fileName></alto>")
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                importer.xml_content(
                    {"path": str(xml)}, {"page.png": ["page.png", "page__2.png"]}
                )

    def test_galaxy_manifest_treats_dataset_names_as_json_data(self):
        from Cheetah.Template import Template

        wrapper = ET.parse(ROOT / "galaxy/interactivetool_escriptorium.xml").getroot()

        class WrappedIdentifier(str):
            def __new__(cls, raw):
                value = super().__new__(cls, "sanitized_name.png")
                value.unsanitized = raw
                return value

        class Dataset:
            ext = "png"
            element_identifier = WrappedIdentifier('page & $(touch marker) "quote".png')

            def __str__(self):
                return "/datasets/dataset_1.dat"

        context = {
            "input": [Dataset()],
            "kraken_models": [],
            "session_archive": None,
            "input_manifest": "/configs/inputs.json",
            "export_format": "alto",
        }
        rendered = str(
            Template(wrapper.find("configfiles/configfile").text, searchList=[context])
        )
        self.assertEqual(
            json.loads(rendered)["inputs"][0]["name"],
            Dataset.element_identifier.unsanitized,
        )
        command = str(Template(wrapper.find("command").text, searchList=[context]))
        self.assertNotIn("touch marker", command)

    def test_galaxy_export_choices_set_the_runtime_configuration(self):
        from Cheetah.Template import Template

        wrapper = ET.parse(ROOT / "galaxy/interactivetool_escriptorium.xml").getroot()
        choices = wrapper.findall("inputs/param[@name='export_format']/option")
        self.assertEqual(
            {option.get("value") for option in choices},
            {"alto", "pagexml", "json", "none"},
        )
        for option in choices:
            value = option.get("value")
            with self.subTest(format=value):
                command = str(
                    Template(
                        wrapper.find("command").text,
                        searchList=[
                            {
                                "input_manifest": "/configs/inputs.json",
                                "session_archive": None,
                                "export_format": value,
                            }
                        ],
                    )
                )
                self.assertIn(
                    f"export STOP_EXPORT={'false' if value == 'none' else 'true'}",
                    command,
                )
                if value != "none":
                    self.assertIn(f"export STOP_EXPORT_FORMAT='{value}'", command)
                self.assertIn('export PROJECT_DIR="$PWD/projects"', command)


class ShutdownTests(unittest.TestCase):
    def test_disabling_document_exports_still_saves_session_and_models(self):
        api = Mock()
        with (
            patch.dict(os.environ, {"STOP_EXPORT": "false"}),
            patch.object(watcher, "status"),
            patch.object(watcher, "read_json", return_value={"phase": "ready"}),
            patch.object(watcher, "process_downloads") as downloads,
            patch.object(watcher, "export_models") as models,
            patch.object(watcher, "create_archive") as archive,
            patch.object(watcher, "write_lock", return_value=nullcontext()),
            patch.object(watcher.subprocess, "run", return_value=Mock(returncode=0)),
        ):
            self.assertTrue(watcher.flush_and_shutdown(api, {}))
        api.list.assert_not_called()
        api.request.assert_not_called()
        downloads.assert_called_once()
        models.assert_called_once()
        archive.assert_called_once()

    def test_text_export_extension_comes_from_download_filename(self):
        api = Mock()
        api.request.return_value.headers = {
            "Content-Disposition": 'attachment; filename="export.txt"'
        }
        self.assertEqual(
            watcher.extension(
                {"mime_type": "application/octet-stream", "file_url": "/file/"},
                api,
                time.monotonic() + 10,
            ),
            ".txt",
        )

    def test_task_api_failure_keeps_session_running(self):
        api = Mock()
        api.wait_tasks.side_effect = requests.ConnectionError(
            "task service unavailable"
        )
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(watcher, "SHUTDOWN", Path(folder) / "shutdown.json"),
            patch.object(watcher, "read_json", return_value={"phase": "ready"}),
            patch.object(watcher.subprocess, "run") as stop,
        ):
            self.assertFalse(watcher.flush_and_shutdown(api, {}))
            self.assertEqual(
                json.loads((Path(folder) / "shutdown.json").read_text())["phase"],
                "failed",
            )
            stop.assert_not_called()

    def test_processing_is_drained_before_export_and_snapshot(self):
        events = []
        api = Mock()
        api.wait_tasks.side_effect = lambda *args: events.append("drain")
        with (
            patch.object(watcher, "status"),
            patch.object(watcher, "read_json", return_value={"phase": "ready"}),
            patch.object(
                watcher,
                "export_document_layers",
                side_effect=lambda *args: events.append("exports"),
            ),
            patch.object(
                watcher,
                "process_downloads",
                side_effect=lambda *args: events.append("downloads"),
            ),
            patch.object(
                watcher,
                "export_models",
                side_effect=lambda *args: events.append("models"),
            ),
            patch.object(
                watcher,
                "create_archive",
                side_effect=lambda *args, **kwargs: events.append("snapshot"),
            ),
            patch.object(watcher, "write_lock", return_value=nullcontext()),
            patch.object(
                watcher.subprocess,
                "run",
                side_effect=lambda *args, **kwargs: (
                    events.append("shutdown") or Mock(returncode=0)
                ),
            ),
        ):
            self.assertTrue(watcher.flush_and_shutdown(api, {}))
        self.assertEqual(
            events,
            [
                "drain",
                "exports",
                "drain",
                "downloads",
                "models",
                "snapshot",
                "shutdown",
            ],
        )


class ArchiveTests(unittest.TestCase):
    def test_restore_rejects_traversal_and_links_before_database_access(self):
        for name, kind in (
            ("../escape", tarfile.REGTYPE),
            ("media/link", tarfile.SYMTYPE),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "bad.tar"
                with tarfile.open(path, "w") as archive:
                    item = tarfile.TarInfo(name)
                    item.type = kind
                    archive.addfile(item, io.BytesIO())
                with patch.object(session_archive.subprocess, "run") as restore:
                    with self.assertRaises(ValueError):
                        session_archive.restore(path)
                    restore.assert_not_called()

    def test_failed_backup_keeps_previous_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.tar"
            path.write_bytes(b"previous checkpoint")
            with patch.object(
                session_archive.subprocess,
                "run",
                side_effect=RuntimeError("pg_dump failed"),
            ):
                with self.assertRaises(RuntimeError):
                    session_archive.create(path)
            self.assertEqual(path.read_bytes(), b"previous checkpoint")
            self.assertEqual(list(Path(folder).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
