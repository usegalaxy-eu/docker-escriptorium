# justfile for docker-escriptorium
# Run `just` (or `just --list`) to see all available recipes.

# Image name and version tag, matching quay.io (see
# .github/workflows/release.yml and the upstream App version, currently 26.10).
# Bump `version` whenever the pinned eScriptorium version changes.
version := "26.10"
image := "quay.io/galaxy/escriptorium"
image_tag := image + ":" + version
# Host port for `just run`
port := "8080"

# default: show this list
default:
    @just --list

# build: build the docker image (cached; heavy layers only rebuild when changed)
build:
    docker build -t {{image_tag}} .

# build-nocache: rebuild everything from scratch (e.g. after base image updates)
build-nocache:
    docker build --no-cache -t {{image_tag}} .

# push: push the image to quay.io (tag {{image_tag}} is pushed as-is)
push:
    docker push {{image_tag}}
    @echo "Pushed {{image_tag}}"

# run: run the full stack locally with test data (usage: just run [extra docker args...])
@run *args:
    # Generate test fixtures (image + ALTO XML + zip) into ./data/import —
    # import-data.py loads them automatically at container startup.
    python3 tests/fixtures.py data/import >/dev/null
    docker rm -f escriptorium >/dev/null 2>&1 || true
    docker run --rm -d --name escriptorium -p {{port}}:80 \
        -v "$(pwd)/data/import:/data/import" \
        -v "$(pwd)/data/export:/data/export" \
        -v "$(pwd)/data/models:/data/models" \
        {{args}} {{image_tag}} >/dev/null
    printf '\neScriptorium is starting. Open:\n\n  http://localhost:{{port}}\n\nLogs: just logs    Stop: just stop\n'

# stop: stop the container started by `just run`
stop:
    docker rm -f escriptorium

# logs: follow the container logs
logs:
    docker logs -f escriptorium

# shell: open a shell inside the running container
shell:
    docker exec -it escriptorium bash

# Run all checks and print a status report with timings and diagnostic logs
test image=image_tag:
    @python3 tests/integration.py {{quote(image)}}
