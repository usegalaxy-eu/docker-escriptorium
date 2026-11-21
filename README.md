# docker-escriptorium

A single-user eScriptorium **v26.10** container for Galaxy Interactive Tools.
It runs PostgreSQL, Redis, Django/uWSGI, Daphne, Celery, Flower and nginx. The
container release is **26.10**. The Python base image is pinned by digest
and the upstream checkout is checked against its expected commit. That commit is recorded in
session archives so incompatible restorations are rejected.

## Build and test

```bash
just build
just test
```

The image is tagged `quay.io/galaxy/escriptorium:26.10`. `just test`
prints named RUN/PASS/FAIL/SKIP checks, elapsed times and a final status table.
Detailed output is saved per check, together with a JSON report and service
logs on failure. Colors are enabled only for a terminal (`NO_COLOR=1` disables
them). Dependent checks are skipped after a failure; independent instances
continue. Any failure or skip returns a nonzero exit status.

Install host-side test dependencies with:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m playwright install chromium
```

The suite renders the actual Galaxy manifest and command, mounts input
`.dat` files read-only, uses the upstream Hebrew print and handwriting images
documented in [tests/assets/README.md](tests/assets/README.md), and checks:

- images, XML and archive imports in a different order from their dependencies;
- upstream segmentation and recognition models, in CoreML and safetensors formats;
- repeated imports without duplicate pages or models;
- browser access, static assets and default authentication;
- public HTTPS host forwarding and normal Django CSRF protection;
- task, document and download pagination beyond the first ten records;
- blocking new writes while draining existing work;
- exports of separate transcription layers and complete model files;
- a full backup/restore round trip preserving projects, metadata, annotations,
  transcription history, image bytes and model versions;
- empty and models-only sessions, custom bootstrap credentials, periodic
  checkpoints, failed-save recovery and the Docker SIGTERM path.

The tests create unique containers and allocate localhost ports automatically.
The image ID is resolved once per run. `TIMEOUT` (default 600 seconds) controls
readiness waits; `SHUTDOWN_TIMEOUT` (default 300) controls test-side shutdown.
Containers and temporary inputs are removed on success, failure or interruption.
Choose a results directory with `TEST_ARTIFACTS_DIR=/tmp/results just test`.
PR CI builds the changed image, runs the same suite and uploads the reports.

For fast regression checks without Docker:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Galaxy deployment

Load `galaxy/interactivetool_escriptorium.xml` and merge the destination and
tool mapping from `galaxy/job_conf.yml.sample` into the site's job configuration.
Enable Galaxy's interactive-tools proxy and container monitor for the destination.
The wrapper uses port **80**, a dedicated interactive hostname, and the versioned
Quay image. Publish that image tag before installing the wrapper on compute nodes.

This all-in-one image needs **UID 0** to start PostgreSQL and the supervisor and
to switch service users. The destination explicitly sets `docker_set_user: "0"`;
application processes run as `escriptorium`. Starting the image with an arbitrary
UID fails immediately with the configuration requirement. Use the site's normal
container isolation and mount only the job's required files.

The proxy must preserve the public `Host` and `X-Forwarded-Proto` headers.
Container nginx forwards the public HTTP/HTTPS scheme instead of replacing it
with its internal HTTP scheme. The stop endpoint uses Django's CSRF protection,
including Origin validation. No placeholder Galaxy origin is baked into the
wrapper; an ordinary same-origin interactive hostname works. Additional trusted
origins, if needed by a site, can be set using `CSRF_TRUSTED_ORIGINS`.

Authentication is disabled by default for Galaxy's private, single-user entry
point. Expose the session only through Galaxy's authenticated proxy. Use
`DISABLE_AUTH=False` when ordinary eScriptorium login is required.

### Inputs

The wrapper writes a JSON manifest containing dataset paths, original filenames
and extensions. Dataset names never become shell code or symlink destinations.
The importer reads the mounted files without changing or moving them. It uploads
images first, then PDF/archives, then dependent XML, and waits for each asynchronous
import to finish before recording a receipt. An import warning about unmatched
images is reported as a failure, rather than silently marking the file imported.

Existing filename extensions are preserved (`page.png` stays `page.png`). Duplicate
image names get distinct upload names. XML that references an ambiguous duplicate
is rejected with an explanation; it cannot safely choose between the images.
Model type detection runs as the application user and uses the original model
extension even when Galaxy's source file ends in `.dat`.

Readiness and failures are visible in the entry page. The document opens only
after data and models have loaded. Empty and models-only launches get a usable
empty document. Receipts live with the media and survive session restoration.

### Saving, stopping and restoring

Click **Save results and stop session**. The server finishes browser writes already
in progress, rejects new writes, waits for startup and queued/running tasks, then
creates the document exports selected in the Galaxy launch form. It waits for the exact export tasks and their
Download records, saves models, and creates and verifies the session archive.
Only then does it stop the services and let the Galaxy job finish normally.

On failure, the app remains available. The dialog shows the error and offers
**Retry saving** or **Resume editing**. Timeouts and API failures cannot be treated
as successful saves. Downloads and archives replace their final paths atomically;
failed transfers and temporary files are excluded from Galaxy discovery.

Galaxy collects three outputs from directories inside the job working tree:

| Output | Default wrapper location | Contents |
|---|---|---|
| Resumable session | `projects/session.tar` | PostgreSQL dump, complete media/model files, checksums and version manifest |
| Document exports | `exports/` | Exports created in the app, plus the automatic exports selected at launch |
| OCR models | `model-exports/` | Current segmentation/recognition model files as individual datasets |

The session archive preserves the database representation of project structure,
all transcription layers and their history, metadata, annotations and model
versions. Select it in **Resume an eScriptorium session** on a later run. Restore
requires a fresh database, the same eScriptorium commit and PostgreSQL major
version. The original bootstrap username is restored automatically; an explicit
conflicting `BOOTSTRAP_USER` is rejected. The startup password configuration
still applies to that account. Members, paths and SHA-256 checksums are validated before restoration;
links, traversal paths and unexpected members are rejected. Supplied additional
inputs can then be imported into the restored session.

The Galaxy option **Automatic document exports on exit** offers ALTO XML
(default), PAGE XML, JSON, or no automatic document exports. ALTO and PAGE XML
produce an archive per document and transcription layer; JSON includes all
layers. This is independent of the complete database-and-media session archive,
which is always saved. Exports created inside the app are collected with any
choice. For standalone containers, `STOP_EXPORT_FORMAT` and `STOP_EXPORT`
provide the same controls; the Galaxy wrapper sets them from the user's choice.

Periodic checkpoints update `session.tar` when the session is idle. Browser
writes briefly wait behind the checkpoint lock. A failed checkpoint retains the
previous archive. Set `SNAPSHOT_INTERVAL=0` to disable periodic checkpoints.

An external cancellation, `docker kill` or node failure cannot guarantee a final
save or Galaxy history collection. The last completed checkpoint and already
saved results remain in the mounted job directory. On SIGTERM the watcher also
attempts a bounded flush of finished exports; it cannot promise to finish running
work or very large transfers within the Docker stop grace period. Use the app's
save-and-stop control for final result collection.

## Standalone use

```bash
docker run --rm -p 8080:80 \
  -v /path/to/inputs:/data/import:ro \
  -v /path/to/models:/data/models:ro \
  -v /path/to/results:/data/export \
  -v /path/to/projects:/data/project \
  -v /path/to/output-models:/data/model-export \
  quay.io/galaxy/escriptorium:26.10
```

`just run` starts a local instance with upstream test images and fixture XML on port 8080.
`just logs` and `just shell` inspect that instance. `just stop` forcibly removes
it; use the UI save-and-stop button to preserve a complete final session first.
`just push` publishes the built, versioned image when explicitly run.

### Configuration

| Variable | Default | Purpose |
|---|---|---|
| `IMPORT_MANIFEST` | unset | Galaxy JSON manifest; otherwise scan import/model directories |
| `IMPORT_DIR` / `MODEL_DIR` | `/data/import` / `/data/models` | Standalone read-only input folders |
| `EXPORT_DIR` | `/data/export` | Completed document exports |
| `PROJECT_DIR` | `/data/project` | Resumable `session.tar` and checkpoints |
| `MODEL_EXPORT_DIR` | `/data/model-export` | Individual OCR model outputs |
| `SESSION_ARCHIVE` | unset | Session archive to restore before migrations and imports |
| `IMPORT_DOCUMENT_NAME` | `Imported document` | Document for new inputs |
| `IMPORT_PROJECT_NAME` | `Imported documents` | Project for new inputs |
| `IMPORT_DOCUMENT_ID` | unset | Explicit target document in an existing/restored database |
| `IMPORT_MAIN_SCRIPT` / `IMPORT_READ_DIRECTION` | `Latin` / `ltr` | New-document defaults |
| `IMPORT_TRANSCRIPTION_NAME` | upstream default | Transcription layer for imported XML |
| `IMPORT_MODEL_JOB` | automatic | Force `segment` or `recognize` when necessary |
| `BOOTSTRAP_USER` / `BOOTSTRAP_PASSWORD` | `admin` / `admin` | Managed bootstrap account; applied at startup |
| `BOOTSTRAP_EMAIL` | `admin@example.com` | Bootstrap email |
| `IMPORT_USER` / `IMPORT_PASSWORD` | bootstrap credentials | Optional alternate API account |
| `DISABLE_AUTH` | `True` | Auto-login as the configured bootstrap user |
| `GALAXY_STOP_BUTTON` | `True` | Enable save/stop controls |
| `GALAXY_ENTRYPOINT_DOCUMENT` | `True` | Open the imported/restored document on entry |
| `SNAPSHOT_INTERVAL` | `300` seconds | Idle checkpoint interval; `0` disables |
| `IMPORT_TIMEOUT` / `RESTORE_TIMEOUT` | `1800` / `600` seconds | Startup import / archive restoration limits |
| `EXPORT_FLUSH_TIMEOUT` | `600` seconds | Final save limit; failure leaves the app running |
| `EXPORT_POLL_INTERVAL` | `10` seconds | Completed-download polling |
| `STOP_EXPORT_FORMAT` | `alto` | Standalone format; Galaxy sets it from the launch form (`alto`/`pagexml`/`json`) |
| `STOP_EXPORT` | `True` | Standalone enable/disable control; Galaxy sets this from the launch form |
| `EXPORT_DELETE_AFTER` | `False` | Delete native Download records after verified local saving |
| `CELERY_CONCURRENCY` / `UWSGI_PROCESSES` | `2` / `2` | Default queue pool / web worker counts |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` | `escriptorium` | Internal database credentials, consistently inherited by all services |

Static assets are collected once at image build time. Runtime initialization
migrates and configures the bootstrap account in one Django process. Fatal
initialization failures stop the job with a nonzero exit status instead of leaving
workers waiting indefinitely.
