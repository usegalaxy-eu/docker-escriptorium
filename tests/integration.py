#!/usr/bin/env python3
"""Container integration tests; share startup only between compatible checks."""

import argparse
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import glob
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixtures import generate


class Failure(RuntimeError):
    pass


class Report:
    """Keep console status concise and preserve detailed output per check."""

    def __init__(self, artifacts, stream=None):
        self.artifacts = artifacts
        self.stream = stream if stream is not None else sys.stdout
        self.color = self.stream.isatty() and "NO_COLOR" not in os.environ
        self.results = []
        self.started = time.monotonic()

    def status(self, status):
        colors = {"RUN": "36", "PASS": "32", "FAIL": "31", "SKIP": "33"}
        padded = f"{status:<4}"
        return f"\033[{colors[status]}m{padded}\033[0m" if self.color else padded

    def show(self, text):
        print(text, file=self.stream, flush=True)

    def record(self, name, status, seconds=0, error=None, log=None):
        result = {
            "name": name,
            "status": status,
            "passed": True if status == "PASS" else False if status == "FAIL" else None,
            "seconds": round(seconds, 2),
        }
        if error:
            result["error"] = str(error)
        if log:
            result["log"] = str(log)
        self.results.append(result)
        self.show(f"  {self.status(status)}  {name}  ({seconds:.1f}s)")
        if error:
            self.show(f"        {str(error).splitlines()[0]}")
        if status == "FAIL" and log:
            self.show(f"        Log: {log}")

    @contextmanager
    def check(self, name):
        self.show(f"  {self.status('RUN')}  {name}")
        started = time.monotonic()
        log = self.artifacts / f"check-{len(self.results) + 1:02d}.log"
        try:
            with (
                log.open("w") as output,
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                yield
        except Exception as exc:
            with log.open("a") as output:
                traceback.print_exc(file=output)
            self.record(name, "FAIL", time.monotonic() - started, exc, log)
            raise
        else:
            self.record(name, "PASS", time.monotonic() - started, log=log)

    def group(self, names, action):
        """Skip dependent checks after a failure, then allow other stacks to run."""
        before = len(self.results)
        try:
            action()
        except Exception as exc:
            done = self.results[before:]
            pending = [name for name in names if name not in {r["name"] for r in done}]
            if not any(r["status"] == "FAIL" for r in done):
                self.record(
                    pending.pop(0) if pending else "Container cleanup",
                    "FAIL",
                    error=exc,
                )
            for name in pending:
                self.record(
                    name, "SKIP", error="An earlier check on this instance failed"
                )

    def finish(self, image):
        seconds = round(time.monotonic() - self.started, 2)
        summary = {"image": image, "seconds": seconds, "results": self.results}
        summary_file = self.artifacts / "summary.json"
        summary_file.write_text(json.dumps(summary, indent=2) + "\n")
        width = max((len(r["name"]) for r in self.results), default=4)
        self.show("\nTest report")
        self.show(f"  {'STATUS':<6} {'CHECK':<{width}}  {'TIME':>8}")
        self.show(f"  {'-' * 6} {'-' * width}  {'-' * 8}")
        for result in self.results:
            self.show(
                f"  {self.status(result['status'])}   {result['name']:<{width}}  {result['seconds']:>7.1f}s"
            )
        counts = {
            status: sum(r["status"] == status for r in self.results)
            for status in ("PASS", "FAIL", "SKIP")
        }
        self.show(
            f"\n  {counts['PASS']} passed, {counts['FAIL']} failed, {counts['SKIP']} skipped in {seconds:.1f}s"
        )
        self.show(
            f"  Results and logs: {self.artifacts}\n  JSON report: {summary_file}"
        )
        return 1 if counts["FAIL"] or counts["SKIP"] else 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def require(condition, message):
    if not condition:
        raise Failure(message)


def wait_for(description, predicate, timeout, interval=1):
    """Use real elapsed time, including the time spent inside each probe."""
    start = time.monotonic()
    deadline = start + timeout
    last = None
    while True:
        try:
            value = predicate()
            if value:
                print(
                    f"[test] {description}: ready after {time.monotonic() - start:.1f}s",
                    flush=True,
                )
                return value
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise Failure(
                f"Timed out after {timeout}s waiting for {description}; last error: {last}"
            )
        time.sleep(min(interval, remaining))


def docker(*args, timeout=30, check=True):
    result = subprocess.run(
        ["docker", *map(str, args)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode:
        raise Failure(
            f"docker {args[0]} failed: {(result.stdout + result.stderr).strip()[-4000:]}"
        )
    return result


class Stack:
    def __init__(
        self,
        image,
        label,
        artifacts,
        *,
        fixtures=True,
        models=False,
        env=None,
        archive=None,
    ):
        self.image = image
        self.label = label
        self.name = f"escriptorium-test-{label}-{uuid.uuid4().hex[:10]}"
        self.artifacts = artifacts / self.name
        self.workdir = Path(tempfile.mkdtemp(prefix=f"{self.name}-"))
        for folder in ("imports", "models", "exports", "projects", "model-exports"):
            (self.workdir / folder).mkdir(parents=True)
        if fixtures:
            generate(str(self.workdir / "imports"))
        if models:
            # Pinned upstream fixtures; no runtime downloads or model generation.
            for name in (
                "fake_seg.mlmodel",
                "overfit_bl_coreml.mlmodel",
                "overfit_bl.safetensors",
            ):
                shutil.copyfile(
                    Path(__file__).parent / "assets" / name,
                    self.workdir / "models" / name,
                )
        self.has_models = models
        self.env = {
            "IMPORT_TRANSCRIPTION_NAME": "Ground truth",
            "IMPORT_DIR": "/galaxy-job/imports",
            "MODEL_DIR": "/galaxy-job/models",
            "EXPORT_DIR": "/galaxy-job/exports",
            "PROJECT_DIR": "/galaxy-job/projects",
            "MODEL_EXPORT_DIR": "/galaxy-job/model-exports",
            "SNAPSHOT_INTERVAL": "0",
            **(env or {}),
        }
        if archive:
            shutil.copyfile(archive, self.workdir / "restore.tar")
            self.env["SESSION_ARCHIVE"] = "/galaxy-job/restore.tar"
        # Render the actual Galaxy configfile. Inputs deliberately have .dat
        # paths and XML precedes its image, as happens with Galaxy datasets.
        from Cheetah.Template import Template
        from types import SimpleNamespace
        import xml.etree.ElementTree as ET

        class Dataset(SimpleNamespace):
            def __str__(self):
                return self.path

        groups = {}
        self.source_files = []
        for key, folder in (("input", "imports"), ("kraken_models", "models")):
            records = []
            for index, path in enumerate(
                sorted((self.workdir / folder).glob("*"), reverse=True)
            ):
                name = path.name
                target = path.with_name(f"dataset_{index}.dat")
                path.rename(target)
                self.source_files.append(target)
                records.append(
                    Dataset(
                        path=f"/galaxy-job/{folder}/{target.name}",
                        element_identifier=name,
                        ext=Path(name).suffix.lstrip("."),
                    )
                )
            groups[key] = records
        wrapper = ET.parse(
            Path(__file__).parent.parent / "galaxy/interactivetool_escriptorium.xml"
        ).getroot()
        manifest = str(
            Template(wrapper.find("configfiles/configfile").text, searchList=[groups])
        )
        (self.workdir / "inputs.json").write_text(manifest)
        self.env["IMPORT_MANIFEST"] = "/galaxy-job/inputs.json"
        for variable in wrapper.findall("environment_variables/environment_variable"):
            self.env.setdefault(variable.attrib["name"], variable.text)
        context = {
            **groups,
            "input_manifest": "/galaxy-job/inputs.json",
            "session_archive": "/galaxy-job/restore.tar" if archive else None,
            "export_format": (
                "none"
                if self.env.get("STOP_EXPORT", "true").lower() in ("false", "0", "no")
                else self.env.get("STOP_EXPORT_FORMAT", "alto")
            ),
        }
        command = str(Template(wrapper.find("command").text, searchList=[context]))
        (self.workdir / "tool-command.sh").write_text(command)

        self.timeout = float(os.getenv("TIMEOUT", "600"))
        self.cookies = http.cookiejar.CookieJar()
        self.browser_http = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )
        self.anonymous_http = urllib.request.build_opener(NoRedirect())
        self.token = None
        self.doc = None
        self.created = False

    def __enter__(self):
        try:
            port = os.getenv("PORT", "")
            args = ["run", "-d", "--name", self.name, "-p", f"127.0.0.1:{port}:80"]
            args += ["-w", "/galaxy-job", "-v", f"{self.workdir}:/galaxy-job"]
            for folder in ("imports", "models"):
                args += ["-v", f"{self.workdir / folder}:/galaxy-job/{folder}:ro"]
            for key, value in self.env.items():
                args += ["-e", f"{key}={value}"]
            # Do not auto-remove: inspect and logs must remain available after shutdown.
            self.created = True
            docker(*args, self.image, "/bin/bash", "/galaxy-job/tool-command.sh")
            binding = json.loads(docker("inspect", self.name).stdout)[0][
                "NetworkSettings"
            ]["Ports"]["80/tcp"][0]
            self.base_url = f"http://127.0.0.1:{binding['HostPort']}"
            print(f"[test] {self.label}: {self.name}, {self.base_url}", flush=True)
            wait_for("application readiness", self.ready, self.timeout)
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            if self.created:
                try:
                    if exc_type:
                        self.diagnostics()
                except Exception as diagnostic_error:
                    print(
                        f"[test] Could not collect all diagnostics: {diagnostic_error}",
                        flush=True,
                    )
                finally:
                    docker("rm", "-f", self.name, check=False)
        finally:
            try:
                shutil.rmtree(self.workdir)
            except PermissionError:
                # A forced stop can interrupt a checkpoint inside a root-owned
                # private temporary directory. Services are already stopped;
                # recover only this test's bind mount, without starting the app.
                docker(
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--user",
                    "0",
                    "--entrypoint",
                    "chown",
                    "-v",
                    f"{self.workdir}:/cleanup",
                    self.image,
                    "-R",
                    f"{os.getuid()}:{os.getgid()}",
                    "/cleanup",
                )
                shutil.rmtree(self.workdir)

    def diagnostics(self):
        self.artifacts.mkdir(parents=True, exist_ok=True)
        for command, filename in (
            ("logs", "supervisor.log"),
            ("inspect", "inspect.json"),
        ):
            result = docker(command, self.name, check=False)
            (self.artifacts / filename).write_text(result.stdout + result.stderr)
        for name in (
            "escriptorium",
            "escriptorium-import",
            "escriptorium-export",
            "uwsgi",
            "daphne",
        ):
            for ext in ("log", "err"):
                docker(
                    "cp",
                    f"{self.name}:/var/log/{name}.{ext}",
                    self.artifacts / f"{name}.{ext}",
                    check=False,
                )
        for state in ("startup.json", "shutdown.json"):
            docker(
                "cp",
                f"{self.name}:/run/escriptorium/{state}",
                self.artifacts / state,
                check=False,
            )
        print(f"[test] Failure logs: {self.artifacts}", flush=True)

    def state(self):
        return json.loads(docker("inspect", self.name).stdout)[0]["State"]

    def ready(self):
        state = self.state()
        require(state["Running"], f"Container exited during startup: {state}")
        try:
            if self.http("/health/ready")[0] == 200:
                return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            pass
        logs = docker("logs", self.name).stdout
        require(
            not any(
                "exited: django-init" in line and "exit status 0;" not in line
                for line in logs.splitlines()
            ),
            "Database initialization failed",
        )
        require("gave up:" not in logs, "A service could not start; see supervisor log")
        return False

    def http(
        self, path, *, method="GET", data=None, token=False, browser=False, headers=None
    ):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = f"Token {self.token}"
        if data is not None:
            data = urllib.parse.urlencode(data, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        opener = self.browser_http if browser else self.anonymous_http
        try:
            response = opener.open(req, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.code, response.read().decode()

    def api(self, path, *, data=None):
        status, body = self.http(
            path, method="POST" if data is not None else "GET", data=data, token=True
        )
        require(status in (200, 201), f"{path}: HTTP {status}: {body[:400]}")
        return json.loads(body)

    def authenticate(self):
        bootstrap = json.loads(
            docker("exec", self.name, "cat", "/run/escriptorium/bootstrap.json").stdout
        )

        def token():
            status, body = self.http(
                "/api/token-auth/",
                method="POST",
                data={
                    "username": self.env.get("BOOTSTRAP_USER") or bootstrap["user"],
                    "password": self.env.get("BOOTSTRAP_PASSWORD", "admin"),
                },
            )
            return json.loads(body).get("token") if status == 200 else None

        self.token = wait_for("API authentication", token, self.timeout)

    def items(self, path):
        items = []
        while path:
            body = self.api(path)
            if isinstance(body, list):
                return items + body
            items.extend(body["results"])
            next_url = body.get("next")
            if next_url:
                parsed = urllib.parse.urlsplit(next_url)
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
            else:
                path = None
        return items

    def wait_import(self):
        self.authenticate()

        def imported():
            # The import script is one-shot. An unsuccessful exit is definitive.
            startup = json.loads(
                docker(
                    "exec", self.name, "cat", "/run/escriptorium/startup.json"
                ).stdout
            )
            require(
                startup.get("phase") != "failed",
                startup.get("message", "Startup import failed"),
            )
            if startup.get("phase") != "ready":
                return False
            self.doc = self.api(f"/api/documents/{startup['document_id']}/")
            tasks = self.items("/api/tasks/")
            failed = [t for t in tasks if t["workflow_state"] in (2, 4)]
            require(not failed, f"Import tasks failed: {failed}")
            return not any(t["workflow_state"] in (0, 1) for t in tasks)

        wait_for("startup import and its tasks", imported, self.timeout)
        parts = self.items(f"/api/documents/{self.doc['pk']}/parts/")
        require(len(parts) == 2, f"Expected two imported pages, got {len(parts)}")
        for part in parts:
            detail = self.api(f"/api/documents/{self.doc['pk']}/parts/{part['pk']}/")
            require(bool(detail["lines"]), f"Page {part['pk']} has no imported lines")
        names = {
            t["name"]
            for t in self.items(f"/api/documents/{self.doc['pk']}/transcriptions/")
        }
        require("Ground truth" in names, f"Missing Ground truth transcription: {names}")
        require(
            all(path.is_file() for path in self.source_files),
            "Read-only inputs were modified or moved",
        )
        receipts = json.loads(
            docker(
                "exec",
                self.name,
                "cat",
                "/home/escriptorium/escriptorium/app/media/.galaxy-imports.json",
            ).stdout
        )
        require(
            {"01-page.png", "02-transcriptions.xml", "03-export.zip"}
            <= {item["name"] for item in receipts.values()},
            "Missing import receipts",
        )
        if self.has_models:
            models = self.items("/api/models/")
            model = next((m for m in models if m["name"] == "fake_seg"), None)
            require(
                model is not None and model["job"] in (1, "Segment"),
                f"Missing segmentation model: {models}",
            )
            require(
                any(item["name"] == "fake_seg.mlmodel" for item in receipts.values()),
                "Missing model import receipt",
            )
            for name in ("overfit_bl_coreml", "overfit_bl"):
                model = next((model for model in models if model["name"] == name), None)
                require(
                    model is not None and model["job"] in (2, "Recognize"),
                    f"Missing recognition model {name}",
                )
        return parts

    def session_fixture(self, action, identifier=None):
        docker(
            "cp",
            Path(__file__).parent / "session_fixture.py",
            f"{self.name}:/tmp/session_fixture.py",
        )
        command = [
            "exec",
            "-e",
            "DJANGO_SETTINGS_MODULE=escriptorium.settings",
            self.name,
            "python3",
            "/tmp/session_fixture.py",
            action,
        ]
        if identifier is not None:
            command.append(str(identifier))
        return json.loads(docker(*command, timeout=60).stdout.strip().splitlines()[-1])

    def wait_stopped(self, timeout):
        state = wait_for(
            "container shutdown",
            lambda: None if self.state()["Running"] else self.state(),
            timeout,
        )
        require(
            state["ExitCode"] == 0 and not state["OOMKilled"],
            f"Unclean container exit: {state}",
        )

    def verify_archive(self):
        archives = list((self.workdir / "exports").glob("*.zip"))
        require(bool(archives), "No ZIP export saved")
        all_xml = b""
        for archive in archives:
            with zipfile.ZipFile(archive) as exported:
                require(exported.testzip() is None, f"Corrupt ZIP: {archive.name}")
                names = exported.namelist()
                for image in ("01-page.png", "page2.png"):
                    require(image in names, f"Missing image {image}: {names}")
                xml = b"\n".join(exported.read(n) for n in names if n.endswith(".xml"))
                all_xml += xml
        for content in (b"Hello world", b"second line", b"zipped"):
            require(content in all_xml, f"Missing transcription {content!r} in exports")
        print(
            f"[test] Verified images, XML and transcription content in {len(archives)} archive(s)",
            flush=True,
        )


def check_open_auth(stack):
    status, _ = stack.http("/")
    require(status in (200, 302), f"Anonymous entry point returned HTTP {status}")
    status, body = stack.http("/projects/", browser=True)
    require(
        status == 200 and 'id="galaxy-stop-button"' in body,
        "Open project page or stop button missing",
    )
    require(
        stack.http("/api/documents/")[0] in (401, 403), "Anonymous API is unprotected"
    )
    require(
        stack.http("/api/documents/", browser=True)[0] == 200,
        "Auto-login session does not authenticate API requests",
    )


def check_closed_auth(stack):
    require(
        stack.http("/api/documents/")[0] in (401, 403),
        "API accessible with DISABLE_AUTH=False",
    )
    require(
        stack.http("/projects/")[0] == 302,
        "Projects must redirect anonymous users to login",
    )
    status, login_page = stack.http("/login/")
    require(
        status == 200 and 'id="galaxy-stop-button"' in login_page,
        "Stop button missing from login page",
    )


def check_proxy(stack):
    stack.http("/projects/", browser=True)
    csrf = next(c.value for c in stack.cookies if c.name == "csrftoken")
    host = "interactive.galaxy.example"
    headers = {
        "Host": host,
        "X-Forwarded-Proto": "https",
        "Origin": "https://" + host,
        "X-CSRFToken": csrf,
    }
    code, body = stack.http(
        "/api/projects/",
        method="POST",
        data={"name": "HTTPS project"},
        browser=True,
        headers=headers,
    )
    require(code == 201, f"HTTPS same-origin write failed: {code}: {body}")
    headers["Origin"] = "https://another.example"
    code, _ = stack.http("/galaxy/stop/", method="POST", browser=True, headers=headers)
    require(code == 403, "The stop endpoint accepted an untrusted Origin")


def check_stop(stack):
    require(
        stack.http("/galaxy/stop/", method="POST")[0] == 403,
        "Stop accepts a request without a CSRF token",
    )
    status, page = stack.http("/projects/", browser=True)
    require(status == 200 and 'id="galaxy-stop-button"' in page, "Stop control missing")
    csrf = next((c.value for c in stack.cookies if c.name == "csrftoken"), None)
    require(csrf is not None, "No CSRF cookie")
    status, _ = stack.http(
        "/galaxy/stop/", method="POST", browser=True, headers={"X-CSRFToken": csrf}
    )
    require(status == 200, f"Stop request returned HTTP {status}")
    if getattr(stack, "pending_task", None):

        def draining():
            code, body = stack.http("/galaxy/status/", browser=True)
            require(code == 200, f"Cannot inspect shutdown: {body}")
            phase = json.loads(body)["shutdown"]["phase"]
            require(
                phase
                not in ("failed", "complete", "exporting", "models", "snapshotting"),
                f"Export started before the older task finished: {phase}",
            )
            return phase == "draining"

        wait_for("draining older task beyond first page", draining, 30)
        code, _ = stack.http(
            "/api/projects/",
            method="POST",
            data={"name": "Too late"},
            browser=True,
            headers={"X-CSRFToken": csrf},
        )
        require(code == 409, "Session still accepts writes while stopping")
        stack.session_fixture("complete", stack.pending_task)
    stack.wait_stopped(float(os.getenv("SHUTDOWN_TIMEOUT", "300")))
    logs = docker("logs", stack.name).stdout
    require(
        "export-watcher (exit status 0" in logs, "Export watcher did not exit cleanly"
    )
    stack.verify_archive()
    from session_archive import verify

    manifest = verify(stack.workdir / "projects/session.tar", compatible=False)
    require(
        any(name.startswith("media/") for name in manifest["files"]),
        "Session archive contains no media",
    )
    if stack.has_models:
        require(
            bool(list((stack.workdir / "model-exports").glob("*"))),
            "OCR models were not saved",
        )
    if getattr(stack, "pending_task", None):
        require(
            len(list((stack.workdir / "exports").glob("pagination-export-*"))) == 11,
            "Downloads beyond the first page were lost",
        )
        contents = b"".join(
            zipfile.ZipFile(path).read(name)
            for path in (stack.workdir / "exports").glob("*.zip")
            for name in zipfile.ZipFile(path).namelist()
            if name.endswith(".xml")
        )
        require(
            b"Revised transcription" in contents,
            "The second transcription layer was lost",
        )


def check_sigterm(stack):
    pk = stack.doc["pk"]
    transcriptions = stack.items(f"/api/documents/{pk}/transcriptions/")
    transcription = next(t for t in transcriptions if t["name"] == "Ground truth")
    stack.api(
        f"/api/documents/{pk}/export/",
        data={
            "file_format": "alto",
            "transcription": transcription["pk"],
            "include_images": "true",
            "region_types": ["Undefined", "Orphan"],
        },
    )
    # A Download row establishes that the archive exists, unlike a task count of zero.
    wait_for(
        "finished export in Downloads API",
        lambda: stack.items("/api/downloads/"),
        stack.timeout,
    )
    require(
        not list((stack.workdir / "exports").iterdir()),
        "Regular polling saved export before SIGTERM test",
    )
    docker("stop", "-t", "20", stack.name, timeout=30)
    state = stack.state()
    require(
        not state["Running"] and not state["OOMKilled"],
        f"Unexpected SIGTERM stop state: {state}",
    )
    # SIGTERM must save the archive even when Docker subsequently reaches its stop deadline.
    stack.verify_archive()
    logfile = stack.workdir / "watcher.log"
    docker("cp", f"{stack.name}:/var/log/escriptorium-export.log", logfile)
    require(
        "Shutdown signal received" in logfile.read_text(),
        "No SIGTERM flush in watcher log",
    )


def check_browser(stack):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        explicit = os.getenv("PLAYWRIGHT_CHROMIUM_PATH")
        if explicit:
            browser = playwright.chromium.launch(executable_path=explicit)
        elif Path(playwright.chromium.executable_path).exists():
            browser = playwright.chromium.launch(
                executable_path=playwright.chromium.executable_path
            )
        else:
            candidates = sorted(
                glob.glob(
                    os.path.expanduser(
                        "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"
                    )
                )
            )
            require(
                bool(candidates),
                "Chromium missing; run python3 -m playwright install chromium",
            )
            print(f"[test] Using cached Chromium: {candidates[-1]}", flush=True)
            browser = playwright.chromium.launch(executable_path=candidates[-1])
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 900})
            page.set_default_timeout(min(stack.timeout * 1000, 30000))
            assets = []
            page.on(
                "response",
                lambda response: (
                    assets.append((response.url, response.status))
                    if "/static/" in response.url
                    else None
                ),
            )
            response = page.goto(stack.base_url, wait_until="domcontentloaded")
            require(
                response.status == 200, f"Entry page returned HTTP {response.status}"
            )
            require(
                f"/document/{stack.doc['pk']}/" in page.url,
                f"Entry point did not open imported document: {page.url}",
            )
            require("escriptorium" in page.title().lower(), "Unexpected document title")
            require(
                "legacy mode" not in page.content().lower(),
                "Bootstrap user is in legacy mode",
            )
            page.wait_for_function(
                "Array.from(document.styleSheets).some(s => s.href && s.href.includes('/static/'))"
            )
            page.wait_for_function("""() => {
                const logo = document.querySelector('#galaxy-stop-button img');
                return logo && logo.complete && logo.naturalWidth > 0;
            }""")
            require(
                any(url.endswith(".css") and status == 200 for url, status in assets),
                "CSS bundle did not load successfully",
            )
            require(
                not [(url, status) for url, status in assets if status >= 400],
                f"Failed static assets: {assets}",
            )
            response = page.goto(
                stack.base_url + "/projects/", wait_until="domcontentloaded"
            )
            require(response.status == 200, "Projects page requires login")
            page.get_by_text("Imported documents", exact=False).first.wait_for()
            result = page.evaluate("""async () => {
                const r = await fetch('/api/documents/');
                return {status: r.status, body: await r.json()};
            }""")
            require(result["status"] == 200, "Browser session cannot access API")
            require(
                any(d["pk"] == stack.doc["pk"] for d in result["body"]["results"]),
                "Imported document missing from browser session",
            )
            data = page.evaluate(
                """async pk => {
                const get = async path => {
                    const r = await fetch(path);
                    if (!r.ok) throw new Error(path + ': HTTP ' + r.status);
                    return r.json();
                };
                const base = '/api/documents/' + pk;
                const parts = (await get(base + '/parts/')).results;
                return {
                    parts: await Promise.all(parts.map(p => get(base + '/parts/' + p.pk + '/'))),
                    transcriptions: await get(base + '/transcriptions/')
                };
            }""",
                stack.doc["pk"],
            )
            require(
                len(data["parts"]) == 2 and all(p["lines"] for p in data["parts"]),
                "Imported lines missing from browser session",
            )
            require(
                any(t["name"] == "Ground truth" for t in data["transcriptions"]),
                "Transcription missing from browser session",
            )
            response = page.goto(
                f"{stack.base_url}/document/{stack.doc['pk']}/",
                wait_until="domcontentloaded",
            )
            require(response.status == 200, "Document dashboard failed")
            page.get_by_text("01-page", exact=False).first.wait_for()
        finally:
            browser.close()


def main(suite=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", nargs="?", default="quay.io/galaxy/escriptorium:26.10")
    parser.add_argument(
        "--suite",
        choices=("all", "import", "noauth", "e2e", "stop", "sigterm", "session"),
        default=suite or "all",
    )
    args = parser.parse_args()
    require(shutil.which("docker"), "docker is required")
    if args.suite in ("all", "e2e"):
        require(
            importlib.util.find_spec("playwright"),
            "Install the playwright Python package for browser tests",
        )
    image = docker("image", "inspect", "--format", "{{.Id}}", args.image).stdout.strip()
    artifacts = Path(
        os.getenv("TEST_ARTIFACTS_DIR")
        or tempfile.mkdtemp(prefix="escriptorium-test-results-")
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    report = Report(artifacts)
    report.show(
        f"\neScriptorium tests\n  Image: {args.image} ({image})\n  Logs:  {artifacts}\n"
    )

    def runner_checks():
        with report.check("Test runner reliability"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(Path(__file__).parent),
                    "-p",
                    "test_*.py",
                    "-v",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            print(result.stdout + result.stderr)
            require(result.returncode == 0, "Test runner checks failed; see check log")

    fixtures = args.suite != "noauth"
    models = args.suite in ("all", "import", "session")
    import_label = "Startup data and model import" if models else "Startup data import"
    interactive_labels = [import_label] if fixtures else ["Default authentication"]
    if fixtures and args.suite in ("all", "noauth"):
        interactive_labels.append("Default authentication")
    if args.suite in ("all", "e2e"):
        interactive_labels.append("Browser UI and static assets")
    if args.suite in ("all", "session"):
        interactive_labels.extend(
            ["HTTPS proxy and CSRF", "Import restart and rich session data"]
        )
    if args.suite in ("all", "stop", "session"):
        interactive_labels.append("Save results and stop session")

    roundtrip = {}

    def interactive():
        with ExitStack() as resources:
            with report.check(interactive_labels[0]):
                stack = resources.enter_context(
                    Stack(
                        image,
                        "interactive",
                        artifacts,
                        fixtures=fixtures,
                        models=models,
                    )
                )
                if fixtures:
                    stack.wait_import()
                else:
                    check_open_auth(stack)
            if fixtures and args.suite in ("all", "noauth"):
                with report.check("Default authentication"):
                    check_open_auth(stack)
            if args.suite in ("all", "e2e"):
                with report.check("Browser UI and static assets"):
                    check_browser(stack)
            if args.suite in ("all", "session"):
                with report.check("HTTPS proxy and CSRF"):
                    check_proxy(stack)
                with report.check("Import restart and rich session data"):
                    docker(
                        "exec", stack.name, "python3", "/import-data.py", timeout=120
                    )
                    stack.wait_import()
                    seeded = stack.session_fixture("seed", stack.doc["pk"])
                    stack.pending_task = seeded["pending_task"]
                    roundtrip["scene"] = seeded["scene"]
            if args.suite in ("all", "stop", "session"):
                with report.check("Save results and stop session"):
                    check_stop(stack)
                    if args.suite in ("all", "session"):
                        roundtrip["archive"] = artifacts / "session.tar"
                        shutil.copyfile(
                            stack.workdir / "projects/session.tar", roundtrip["archive"]
                        )

    def protected():
        with report.check("Protected authentication"):
            with Stack(
                image,
                "protected",
                artifacts,
                fixtures=False,
                env={
                    "DISABLE_AUTH": "False",
                    "BOOTSTRAP_USER": "reviewer",
                    "BOOTSTRAP_PASSWORD": "quote'&literal$",
                    "SNAPSHOT_INTERVAL": "1",
                },
            ) as stack:
                check_closed_auth(stack)
                stack.authenticate()
                wait_for(
                    "empty-session checkpoint with custom credentials",
                    lambda: (stack.workdir / "projects/session.tar").is_file(),
                    stack.timeout,
                )
                from session_archive import verify

                verify(stack.workdir / "projects/session.tar", compatible=False)
                roundtrip["custom_archive"] = artifacts / "custom-session.tar"
                shutil.copyfile(
                    stack.workdir / "projects/session.tar", roundtrip["custom_archive"]
                )

    def restored():
        if not roundtrip.get("archive"):
            report.record(
                "Restore complete session",
                "SKIP",
                error="The source session was not saved",
            )
            return
        with report.check("Restore complete session"):
            with Stack(
                image,
                "restore",
                artifacts,
                fixtures=False,
                archive=roundtrip["archive"],
            ) as stack:
                stack.wait_import()
                require(
                    stack.session_fixture("inspect") == roundtrip["scene"],
                    "Restored project, annotations, layers, history or model bytes changed",
                )

    def models_only():
        with report.check("Models-only startup"):
            with Stack(
                image,
                "models-only",
                artifacts,
                fixtures=False,
                models=True,
                archive=roundtrip.get("custom_archive"),
            ) as stack:
                stack.authenticate()

                def ready():
                    data = json.loads(
                        docker(
                            "exec", stack.name, "cat", "/run/escriptorium/startup.json"
                        ).stdout
                    )
                    require(
                        data["phase"] != "failed", data.get("message", "Import failed")
                    )
                    return data if data["phase"] == "ready" else None

                state = wait_for("models-only document", ready, stack.timeout)
                require(
                    stack.api(f"/api/documents/{state['document_id']}/")["parts_count"]
                    == 0,
                    "Expected an empty document",
                )
                require(
                    len(stack.items("/api/models/")) == 3, "An input model is missing"
                )
                if roundtrip.get("custom_archive"):
                    account = json.loads(
                        docker(
                            "exec",
                            stack.name,
                            "cat",
                            "/run/escriptorium/bootstrap.json",
                        ).stdout
                    )
                    require(
                        account["user"] == "reviewer",
                        "The restored bootstrap owner changed",
                    )
                require(
                    stack.http("/", browser=True)[0] == 302,
                    "Models-only session remains stuck on the loading page",
                )

    def failed_save():
        with report.check("Failed save can resume editing"):
            with Stack(
                image, "failed-save", artifacts, env={"STOP_EXPORT_FORMAT": "invalid"}
            ) as stack:
                stack.wait_import()
                from playwright.sync_api import sync_playwright

                with sync_playwright() as p:
                    binary = (
                        os.getenv("PLAYWRIGHT_CHROMIUM_PATH")
                        or p.chromium.executable_path
                    )
                    if not Path(binary).exists():
                        binary = sorted(
                            glob.glob(
                                os.path.expanduser(
                                    "~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"
                                )
                            )
                        )[-1]
                    browser = p.chromium.launch(executable_path=binary)
                    try:
                        page = browser.new_page()
                        page.goto(stack.base_url + "/projects/")
                        page.get_by_role(
                            "button", name="Save results and stop session"
                        ).click()
                        page.get_by_role("button", name="Resume editing").wait_for(
                            timeout=60000
                        )
                        require(
                            stack.state()["Running"],
                            "Failed export destroyed the session",
                        )
                        page.get_by_role("button", name="Resume editing").click()
                        page.get_by_role(
                            "button", name="Save results and stop session"
                        ).wait_for(state="visible")
                        state = page.evaluate(
                            "fetch('/galaxy/status/').then(r => r.json())"
                        )
                        require(
                            state["shutdown"]["phase"] == "running",
                            "Resume did not unlock the session",
                        )
                    finally:
                        browser.close()

    def sigterm():
        with report.check("Finished exports on SIGTERM"):
            with Stack(
                image, "sigterm", artifacts, env={"EXPORT_POLL_INTERVAL": "3600"}
            ) as stack:
                stack.wait_import()
                check_sigterm(stack)

    if args.suite == "all":
        report.group(["Test runner reliability"], runner_checks)
    if args.suite != "sigterm":
        report.group(interactive_labels, interactive)
    if args.suite in ("all", "noauth"):
        report.group(["Protected authentication"], protected)
    if args.suite in ("all", "session"):
        report.group(["Restore complete session"], restored)
    if args.suite == "all":
        report.group(["Models-only startup"], models_only)
        report.group(["Failed save can resume editing"], failed_save)
    if args.suite in ("all", "sigterm"):
        report.group(["Finished exports on SIGTERM"], sigterm)
    return report.finish(image)


def interrupted(signum, frame):
    # Unwind active Stack contexts so CI cancellation also removes containers.
    raise KeyboardInterrupt


def cli(suite=None):
    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        return main(suite)
    except KeyboardInterrupt:
        print("[test] Interrupted; test containers cleaned up", file=sys.stderr)
        return 130
    except (Failure, subprocess.TimeoutExpired) as exc:
        print(f"[test] FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(cli())
