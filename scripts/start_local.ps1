param([switch]$OfflineBuild)
$ErrorActionPreference = 'Stop'
$collectorRoot = Split-Path -Parent $PSScriptRoot
Push-Location $collectorRoot
try {
    if ($OfflineBuild) {
        New-Item -ItemType Directory -Force -Path '.wheels' | Out-Null
        docker run --rm --dns 1.1.1.1 --dns 8.8.8.8 -v "${collectorRoot}:/source" python:3.12-slim-bookworm pip download --only-binary=:all: -r /source/requirements.lock -d /source/.wheels
        if ($LASTEXITCODE) { throw 'Dependency download failed' }
        docker compose build --build-arg PIP_NO_INDEX=1 api
    } else {
        docker compose build api
    }
    if ($LASTEXITCODE) { throw 'Build failed. If Docker build DNS fails, use -OfflineBuild.' }
    docker compose run --rm migrate
    if ($LASTEXITCODE) { throw 'Collector schema migration failed' }
    docker compose run --rm api python scripts/preflight.py
    if ($LASTEXITCODE) { throw 'Preflight failed' }
    docker compose run --rm api python scripts/login.py
    if ($LASTEXITCODE) { throw 'Login verification failed; workers have not been started' }
    docker compose up -d --no-build
    if ($LASTEXITCODE) { throw 'Service startup failed' }
    docker compose ps
} finally {
    Pop-Location
}
