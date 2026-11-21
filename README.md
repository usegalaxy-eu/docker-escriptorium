# docker-escriptorium

All-in-one Docker image for [eScriptorium](https://gitlab.com/scripta/escriptorium)
(the app source is cloned and pinned to **v26.10** at build time). A single
container runs PostgreSQL, Redis, uWSGI (Django), Daphne (websockets), the
Celery workers, Flower and nginx.

## Build

The repo ships a [justfile](https://github.com/casey/just) that
auto-documents all common tasks — run `just` to list them:

```bash
just                  # list all recipes with descriptions
just build            # docker build -t quay.io/galaxy/escriptorium:26.10 .
just run              # start the stack on port 8080 (./data/import, ./data/export, ./data/models)
just stop             # stop it again
just test             # run all end-to-end tests (~15 min)
just push 1.0.0       # tag & push to quay.io/galaxy/escriptorium:1.0.0
```

Equivalent manual commands:

```bash
just build
```

## Run

For local development, `just run` starts the stack on port 8080, generates
the test fixtures into `./data/import` (which are auto-imported at startup
into *Imported documents → Imported document*) and prints the exact docker
command it runs:

```bash
just run          # http://localhost:8080 — logs: just logs, stop: just stop
```

Manually:

```bash
docker run -d -p 80:80 --name escriptorium quay.io/galaxy/escriptorium:26.10
```

Then open <http://localhost/> and log in with the bootstrap superuser:

| Setting  | Default | Override with    |
|----------|---------|------------------|
| username | `admin` | `BOOTSTRAP_USER` |
| password | `admin` | `BOOTSTRAP_PASSWORD` |
| email    | `admin@example.com` | `BOOTSTRAP_EMAIL` |

> **Change the bootstrap password in production** (or set
> `BOOTSTRAP_PASSWORD` before the first start — the user is created once, on
> the first boot).

Other useful environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | `please-change-me` | Django secret key |
| `CSRF_TRUSTED_ORIGINS` | `http://localhost` | Comma-separated origins the site is served on, e.g. `https://escriptorium.example.org` |
| `DEBUG` | `False` | Django debug mode |
| `DISABLE_AUTH` | `True` | **Disable login**: every visitor is automatically logged in as the bootstrap superuser (see below). This image defaults to Galaxy-IT behaviour; set `DISABLE_AUTH=False` for normal login |
| `GALAXY_STOP_BUTTON` | `True` | Show the floating **"Stop session and export to Galaxy"** button (see below); set `False` to hide it |
| `GALAXY_ENTRYPOINT_DOCUMENT` | `True` | Redirect `GET /` to the startup-imported document (see below); set `False` to serve the normal homepage |

### Authentication (login off by default)

This image is meant to run as a Galaxy Interactive Tool: the container is
single-user and only reachable through Galaxy's proxied entry point, so
**login is disabled by default**. Every anonymous request is automatically
logged in as the bootstrap superuser — the site and the REST API are usable
without any credentials, and a real session is established so websockets
work too.

For a standalone, potentially multi-user deployment, restore normal login
behaviour with:

```bash
docker run -d -p 80:80 -e DISABLE_AUTH=False quay.io/galaxy/escriptorium:26.10
```

> ⚠️ **Warning:** with login disabled, this grants full admin access to
> *anyone who can reach the site*. Only run it on trusted/private networks
> (a Galaxy interactive tool container satisfies this: its ports are not
> published, users reach it only through the Galaxy proxy).

## Auto-loading files at startup (import-data)

Any file mounted into `/data/import` is imported through the REST API right
after the container boots, so the content is already loaded when a user first
visits the website:

```bash
docker run -d -p 80:80 \
  -v /path/to/your/files:/data/import \
  quay.io/galaxy/escriptorium:26.10
```

Supported files (processed in alphabetical order):

- **Images** (`.png`, `.jpg`, `.jpeg`, `.tif`, `.tiff`, `.webp`, …) — uploaded
  as pages of the target document.
- **PDF** (`.pdf`) — pages are extracted and imported as images.
- **ALTO / PAGE / Abbyy XML** (`.xml`) — imports segmentation and/or
  transcriptions into the target document. To match the XML to an already
  uploaded image, ALTO files must contain a
  `Description/sourceImageInformation/fileName` tag with the image's
  filename (PAGE files use the `imageFilename` attribute on the `Page`
  tag), and the root element must declare a valid
  `xsi:schemaLocation`.
- **Zip archives** (`.zip`) — zips of images and/or XML files; an eScriptorium
  *document export archive* (images + XML) can be imported this way as well.

After a successful import the file is moved to `/data/import/.imported`, so
restarting the container will not import the same file twice.

### Kraken models

Any Kraken model (`.mlmodel` or `.safetensors`) mounted into `/data/models`
is uploaded as an OCR model right after the container boots, so it is
available in the document's process panel immediately:

```bash
docker run -d -p 80:80 \
  -v /path/to/your/models:/data/models \
  quay.io/galaxy/escriptorium:26.10
```

Whether a model is a **segmentation** or **recognition** model is detected
automatically by loading it with the kraken library (the same detection the
web upload form uses). Force the type with `IMPORT_MODEL_JOB=segment` or
`IMPORT_MODEL_JOB=recognize` if detection is ambiguous. Models whose name
already exists are skipped (restarts are idempotent); successfully imported
models are moved to `/data/models/.imported`.

### Choosing the target document

By default the script creates (or reuses) a project named *Imported
documents* containing a document named *Imported document*, and imports
everything into it. This can be configured with environment variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `IMPORT_DOCUMENT_ID` | — | Import into an **existing** document by id (skips project/document creation) |
| `IMPORT_PROJECT_NAME` | `Imported documents` | Project to create/reuse |
| `IMPORT_DOCUMENT_NAME` | `Imported document` | Document to create/reuse |
| `IMPORT_READ_DIRECTION` | `ltr` | Reading direction for new documents (`ltr`/`rtl`) |
| `IMPORT_MAIN_SCRIPT` | `Latin` | Main script for new documents (required by the v26.10 API; must match a name from the seeded script list, e.g. `Latin`, `Greek`, `Arabic`) |
| `IMPORT_TRANSCRIPTION_NAME` | — | Name of the transcription created from XML imports |
| `IMPORT_DIR` | `/data/import` | Directory to scan |
| `MODEL_DIR` | `/data/models` | Directory with Kraken models to upload |
| `MODEL_DIR_WAIT` | `60` | Seconds to wait for a late-created `MODEL_DIR` |
| `IMPORT_MODEL_JOB` | auto | Force the model type: `segment` or `recognize` (default: detected with the kraken library) |

Example:

```bash
docker run -d -p 80:80 \
  -v /path/to/your/files:/data/import \
  -e IMPORT_DOCUMENT_NAME="Gallery of Fashion" \
  -e IMPORT_MAIN_SCRIPT="Latin" \
  -e IMPORT_TRANSCRIPTION_NAME="Ground truth" \
  quay.io/galaxy/escriptorium:26.10
```

The import log is written to `/var/log/escriptorium-import.log` inside the
container (`docker logs` shows it too, via supervisord's stdout log).

## Testing

One command runs the runner's reliability checks and all container integration
checks against a locally built image:

```bash
just build
just test
# Optional: select another locally available image
just test quay.io/galaxy/escriptorium:26.10
```

Each check prints RUN followed by PASS, FAIL, or SKIP and its elapsed time.
The final report lists every check, totals and the location of diagnostic logs.
Status labels are colored in a terminal; redirected output stays plain text.
Set `NO_COLOR=1` to disable colors. Detailed output from each check is saved
in its log, keeping the status report readable.

The checks cover:

- test-runner timeout handling, cleanup, pagination and failure reporting;
- startup import of images, ALTO XML, an image/XML archive and a segmentation
  model, including imported lines, the `Ground truth` transcription and the
  `.imported` markers;
- default auto-login, session cookies and anonymous API protection;
- browser access to the project, document, pages and transcriptions, with
  successful static-asset loading and the current document dashboard;
- the CSRF-protected stop button, a clean container exit and the saved archive's
  CRC, both images and all expected transcription strings;
- protected access with `DISABLE_AUTH=False`;
- preservation of a finished export on `docker stop`, before the regular
  export poll can save it. This tests SIGTERM flushing; it does not establish
  how a Galaxy runner cancels a job or preserves in-progress work.

Compatible checks share one instance; protected authentication and SIGTERM
use separate instances. That keeps the full run to three stack startups.
When a check fails, dependent checks are marked SKIP and independent checks
continue. A failure or skipped check makes the command exit nonzero.

Requirements: `docker` and `python3`, plus `playwright` for browser checks
(`pip install playwright` and `python3 -m playwright install chromium`).
The runner prefers Playwright's matching Chromium, falls back to an existing
Chromium cache, or uses `PLAYWRIGHT_CHROMIUM_PATH` when set. Tests use the
image's default `admin`/`admin` bootstrap account.

Containers have unique names and automatically allocated localhost ports, so
runs can coexist. Set `PORT` to use a fixed port. The image ID is resolved once
so all checks use the same image even if its tag changes. `TIMEOUT` (default
`600` seconds) controls readiness waits; `SHUTDOWN_TIMEOUT` (default `300`
seconds) controls stop-button shutdown. HTTP requests and Docker commands also
have timeouts. Test containers and temporary inputs are removed on completion,
failure or interruption.

Each run prints its results directory, containing per-check logs and
`summary.json` with statuses, timings and the image ID. Failed checks also save
container inspection and service logs. Choose a persistent destination with:

```bash
TEST_ARTIFACTS_DIR=/tmp/escriptorium-results just test
```

For a quick check of the runner itself without Docker:

```bash
python3 -m unittest discover -s tests -p test_harness.py
```

## Galaxy Interactive Tool integration

The image can be used as a [Galaxy Interactive Tool](https://docs.galaxyproject.org/en/master/admin/interactive_tools.html)
(GxIT) — see `galaxy/interactivetool_escriptorium.xml` for a ready-to-adapt
tool definition. The mechanism, following the same patterns as the other
interactive tools (QuPath, Napari, Jupyter, ...):

- **Dynamically located folders** — every interactive tool gets its own job
  working directory. The tool command creates `./imports`, `./exports` and
  `./models` inside it and points the app at them at startup via
  `export IMPORT_DIR=$PWD/imports`, `export EXPORT_DIR=$PWD/exports` and
  `export MODEL_DIR=$PWD/models`
  (in the tool XML: `\$PWD` — Cheetah eats unescaped `$`). Both scripts read
  the variables when the stack starts, so any folder works — the defaults
  (`/data/import`, `/data/export`, `/data/models`) are only fallbacks.
  `import-data.py` waits briefly (`IMPORT_DIR_WAIT` / `MODEL_DIR_WAIT`,
  default 60 s each) if a directory is created late, and
  `export-watcher.py` creates `EXPORT_DIR` if missing.
- **Inputs** — the tool's `<command>` runs *inside* the container with `pwd`
  set to Galaxy's job working directory. Galaxy datasets are symlinked into
  `./imports/`, which is exposed as `IMPORT_DIR`; the normal startup
  auto-import (see above) then imports them into a document. Kraken models
  (`.mlmodel`/`.safetensors`) are symlinked into `./models/`
  (`MODEL_DIR`) and uploaded as OCR models at startup.
- **The app** — login is disabled by default (auto-login as the bootstrap
  superuser; the tool sets `DISABLE_AUTH=True` explicitly anyway) and the
  whole stack starts with `exec /usr/bin/supervisord -n`; Galaxy proxies the
  entry point (port 80) into an iframe. For this to work,
  `docker-entrypoint.sh` executes the tool command instead of the default
  stack whenever the container is started *with* a command.
- **Entry point = the document** — the iframe loads `/`, which redirects to
  the startup-imported document (`IMPORT_DOCUMENT_NAME`, default
  *Imported document*; the GxIT sets it to *Galaxy import*) as soon as it
  exists — the user lands directly in the document instead of the homepage.
  While the import is still running, a self-reloading *"Preparing your
  document…"* page is served. Disable with `GALAXY_ENTRYPOINT_DOCUMENT=False`.
  The image also switches the bootstrap user to the new UI on a fresh
  database: upstream's migrations leave it in *legacy mode*, which would
  blank the document dashboard the entry point lands on.
- **Outputs (result pickup)** — eScriptorium exports are asynchronous; the
  built-in `export-watcher` service polls the Downloads API
  (`GET /api/downloads/`) and saves every finished export archive into
  `EXPORT_DIR`. The tool command points `EXPORT_DIR` at `./exports/` inside
  the job working directory, and the tool XML declares:

  ```xml
  <collection name="exports" type="list" label="eScriptorium exports">
      <discover_datasets pattern="__name_and_ext__" directory="exports" />
  </collection>
  ```

  When the user ends the interactive session, Galaxy harvests that folder
  into the history as a dataset collection. Files are named after the
  export label with a proper extension; already-saved exports are tracked
  (by fingerprint, in `/tmp/export-watcher-state.json`) so nothing is
  duplicated on restarts.

### User story

1. **Launch the tool.** The entry point opens directly in the *Galaxy import*
   document: the Galaxy datasets are imported at startup and `GET /`
   redirects to that document (`GALAXY_ENTRYPOINT_DOCUMENT`). While the
   import is still running, a self-reloading *"Preparing your document…"*
   page is shown instead — no need to do anything, it switches to the
   document as soon as the import is done.
2. **Work.** Segment, transcribe and train as usual — everything is saved in
   the running session.
3. **(Optional) Export individual results while working.** Use the *Export*
   button on the document dashboard; every finished archive is picked up by
   the watcher and lands in the Galaxy output folder immediately.
4. **Close the app.** Click the red **Stop session and export to Galaxy** button
   (bottom right). It triggers a **full export of every document** in the
   session (all transcriptions + images — a re-importable backup), waits for
   running tasks, saves everything into the output folder and shuts the
   session down.
5. **Collect.** When the session ends, Galaxy harvests the output folder
   into the history: the per-result exports plus the automatic full-document
   backup (`Export <document name>.zip`).

The full backup is exported in ALTO format by default; configure with
`STOP_EXPORT_FORMAT` (`alto`, `pagexml`, `json`) or disable with
`STOP_EXPORT=no`.

### Saving exports into a folder

Every export triggered from the eScriptorium UI (document dashboard →
*Export*, or the *Downloads* page) is picked up by the built-in
**export watcher** and saved into `EXPORT_DIR` (default `/data/export`)
as soon as it is finished — no manual download step needed. Files are
named after the export label with the proper extension; already-saved
exports are tracked (by fingerprint, in `/tmp/export-watcher-state.json`)
so nothing is duplicated on restarts. Mount the folder anywhere you want
the results:

```bash
docker run -d -p 80:80 -v /path/to/results:/data/export quay.io/galaxy/escriptorium:26.10
```

### Stopping the session from the user side

The floating red **"Stop session and export to Galaxy"** button (bottom right corner,
enabled by default; set `GALAXY_STOP_BUTTON=False` to hide it) and the
`POST /galaxy/stop/` endpoint behind it work together. Clicking the button:

1. triggers a full export of every document the user can access (all
   transcriptions + images — re-importable with the startup auto-import),
   unless `STOP_EXPORT=no`,
2. waits for running tasks (e.g. an in-flight export) to finish — up to
   `EXPORT_FLUSH_TIMEOUT` seconds (default 300),
3. saves all pending exports into `EXPORT_DIR`,
4. shuts the whole container down gracefully (`supervisorctl shutdown`).

For a Galaxy interactive tool this means the user controls when the session
ends: the moment the container exits, Galaxy harvests `exports/` into the
history. The button is injected into every HTML page by the
`escriptorium.galaxy_integration` middleware (enabled by default for this
image, which is Galaxy-first; set `GALAXY_STOP_BUTTON=False` to hide it,
e.g. for a standalone container where the app should not be stoppable from
the web UI). The stop endpoint is CSRF-protected and requires a logged-in
user.

### Closing the session from the Galaxy side (docker stop)

If the session is ended without the button — Galaxy tears the container
down, or someone runs `docker stop` — supervisord forwards the SIGTERM and
the export watcher catches it: it **quick-flushes every export that has
already finished** into `EXPORT_DIR` before exiting (verified by
the SIGTERM check in `just test`). Exports that are still *running* at that moment
cannot be completed within docker's stop grace period — use the
**Stop session and export to Galaxy** button when you want the guaranteed full backup
(final export of all documents + wait for running tasks).

| Close path | Finished exports | Running exports | Full backup |
|---|---|---|---|
| **Stop session and export to Galaxy** button | saved | waited for | yes |
| Galaxy-side close / `docker stop` | saved (SIGTERM flush) | lost | no |
| `docker kill` / crash | lost | lost | no |

Standalone (non-Galaxy) use of the watcher also works: set `EXPORT_DIR` to
any mounted folder and exported archives will simply appear there.

Relevant environment variables: `EXPORT_DIR` (default `/data/export`),
`EXPORT_POLL_INTERVAL` (default `10` seconds), `EXPORT_DELETE_AFTER`
(delete each download from eScriptorium after saving, default off),
`STOP_EXPORT` / `STOP_EXPORT_FORMAT` (automatic full-document export on
shutdown), and the shared `IMPORT_USER`/`IMPORT_PASSWORD` API credentials.
On the import side: `IMPORT_DIR` (default `/data/import`) and
`IMPORT_DIR_WAIT` (seconds to wait for a late-created import directory,
default `60`).

## Manual import (for reference)

Besides the automatic startup import, files can always be loaded into a
running instance:

- **Web UI** — open a document, go to the *Images* tab: drag-and-drop images,
  or *Import → PDF / IIIF / METS / XML (ALTO, PAGE, Abbyy)*.
- **REST API** — `POST /api/documents/<id>/import/` (`mode=xml|pdf|iiif|mets`
  with `upload_file`), `POST /api/documents/<id>/parts/` (image upload),
  authenticate with `POST /api/token-auth/`.

## Services inside the container

| Service | Port | Log |
|---------|------|-----|
| nginx (web) | 80 | `/var/log/nginx/` |
| uWSGI (Django) | 8000 (internal) | `/var/log/uwsgi.log` |
| Daphne (websockets) | 5000 (internal) | `/var/log/daphne.log` |
| Flower (celery monitor) | 5555 | — |
| PostgreSQL | 5432 (internal) | supervisord |
| Redis | 6379 (internal) | supervisord |
| Celery workers | — | supervisord |
| Import job | — | `/var/log/escriptorium-import.log` |
| Export watcher | — | `/var/log/escriptorium-export.log` |

Celery queues (v26.x): `default`, `low-priority` and `live` are served by one
worker; training runs on the `gpu` worker (CPU is fine, no GPU required);
`jvm`/`intensive-inference` tasks (text alignment via pyspark, heavy
inference) have their own worker.
