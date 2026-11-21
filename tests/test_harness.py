"""Fast checks for failure handling in the container test runner (no Docker)."""

from pathlib import Path
import io
import json
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

from integration import Failure, Report, Stack, cli, wait_for


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.results = tempfile.TemporaryDirectory()
        self.addCleanup(self.results.cleanup)
        self.output = io.StringIO()
        self.report = Report(Path(self.results.name), self.output)

    def test_failed_check_skips_dependents_and_returns_failure(self):
        def checks():
            with self.report.check("Import"):
                print("Detailed import output")
                raise Failure("Import failed")

        self.report.group(["Import", "Browser"], checks)
        self.assertEqual(self.report.finish("test-image"), 1)
        summary = json.loads((Path(self.results.name) / "summary.json").read_text())
        self.assertEqual([r["status"] for r in summary["results"]], ["FAIL", "SKIP"])
        self.assertIn(
            "Detailed import output", Path(summary["results"][0]["log"]).read_text()
        )
        self.assertIn("1 failed, 1 skipped", self.output.getvalue())

    def test_independent_checks_continue_after_failure(self):
        def failed():
            with self.report.check("Import"):
                raise Failure("Import failed")

        def independent():
            with self.report.check("Protected authentication"):
                pass

        self.report.group(["Import"], failed)
        self.report.group(["Protected authentication"], independent)
        self.assertEqual([r["status"] for r in self.report.results], ["FAIL", "PASS"])
        self.assertEqual(self.report.finish("test-image"), 1)

    def test_cleanup_failure_cannot_produce_a_passing_report(self):
        def checks():
            with self.report.check("Import"):
                pass
            raise Failure("Cleanup failed")

        self.report.group(["Import"], checks)
        self.assertEqual([r["status"] for r in self.report.results], ["PASS", "FAIL"])
        self.assertEqual(self.report.finish("test-image"), 1)


class WaitingTests(unittest.TestCase):
    def test_request_time_counts_towards_deadline(self):
        # A slow probe can consume the entire timeout before the first sleep.
        with (
            patch("integration.time.monotonic", side_effect=[0, 2]),
            patch("integration.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(Failure, "Timed out"):
                wait_for("a slow service", lambda: False, timeout=1)
        sleep.assert_not_called()

    def test_connection_refused_is_retried(self):
        probe = Mock(side_effect=[urllib.error.URLError("connection refused"), "ready"])
        with patch("integration.time.sleep"):
            self.assertEqual(wait_for("a new service", probe, timeout=1), "ready")

    def test_definitive_failure_is_not_retried(self):
        probe = Mock(side_effect=Failure("container exited"))
        with self.assertRaisesRegex(Failure, "container exited"):
            wait_for("a dead service", probe, timeout=60)
        self.assertEqual(probe.call_count, 1)

    def test_interruption_returns_nonzero(self):
        with patch("integration.main", side_effect=KeyboardInterrupt):
            self.assertEqual(cli(), 130)


class StackTests(unittest.TestCase):
    def setUp(self):
        self.results = tempfile.TemporaryDirectory()
        self.addCleanup(self.results.cleanup)
        self.stack = Stack(
            "test-image", "unit", Path(self.results.name), fixtures=False
        )
        self.addCleanup(
            lambda: (
                self.stack.__exit__(None, None, None)
                if self.stack.workdir.exists()
                else None
            )
        )

    def test_cleanup_survives_diagnostic_failure(self):
        self.stack.created = True
        with (
            patch.object(
                self.stack, "diagnostics", side_effect=RuntimeError("log copy failed")
            ),
            patch("integration.docker") as docker,
        ):
            self.stack.__exit__(Failure, Failure("test failed"), None)
        docker.assert_called_once_with("rm", "-f", self.stack.name, check=False)
        self.assertFalse(self.stack.workdir.exists())

    def test_cleanup_recovers_interrupted_root_owned_checkpoint(self):
        with (
            patch(
                "integration.shutil.rmtree",
                side_effect=[PermissionError("checkpoint in progress"), None],
            ),
            patch("integration.docker") as docker,
        ):
            self.stack.__exit__(None, None, None)
        self.assertEqual(docker.call_count, 1)
        self.assertIn("--entrypoint", docker.call_args.args)
        self.assertIn(f"{self.stack.workdir}:/cleanup", docker.call_args.args)

    def test_pagination_includes_later_pages(self):
        self.stack.api = Mock(
            side_effect=[
                {
                    "results": [{"pk": 1}],
                    "next": "http://localhost/api/documents/?page=2",
                },
                {"results": [{"pk": 2}], "next": None},
            ]
        )
        self.assertEqual(self.stack.items("/api/documents/"), [{"pk": 1}, {"pk": 2}])
        self.stack.api.assert_called_with("/api/documents/?page=2")

    def test_failed_import_stops_without_waiting_for_timeout(self):
        self.stack.authenticate = Mock()
        responses = [
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {"phase": "failed", "message": "Startup importer failed"}
                ),
                stderr="",
            )
        ]
        with (
            patch("integration.docker", side_effect=responses),
            patch("integration.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(Failure, "Startup importer failed"):
                self.stack.wait_import()
        sleep.assert_not_called()

    def test_startup_failure_is_detected_even_when_nginx_is_unreachable(self):
        self.stack.state = Mock(return_value={"Running": True})
        self.stack.http = Mock(side_effect=urllib.error.URLError("connection refused"))
        logs = subprocess.CompletedProcess(
            [], 0, stdout="exited: django-init (exit status 2; not expected)", stderr=""
        )
        with patch("integration.docker", return_value=logs):
            with self.assertRaisesRegex(Failure, "Database initialization failed"):
                self.stack.ready()

    def test_nonempty_zip_is_insufficient(self):
        archive = self.stack.workdir / "exports/incomplete.zip"
        with zipfile.ZipFile(archive, "w") as exported:
            exported.writestr("01-page.png", b"image")
            exported.writestr("page2.png", b"image")
            exported.writestr("page.xml", b"<text>Hello world</text>")
        with self.assertRaisesRegex(Failure, "Missing transcription"):
            self.stack.verify_archive()


if __name__ == "__main__":
    unittest.main()
