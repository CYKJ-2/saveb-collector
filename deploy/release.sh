#!/usr/bin/env bash
# Deploy immutable images. Rollback restores code/Compose, never reverses database migrations.
set -Eeuo pipefail
umask 077
root="${1:?deployment root required}"
target="${2:?40-character commit required}"
mode="${3:-deploy}"
[[ "$root" =~ ^/[a-zA-Z0-9_/-]+$ && "$root" != / && "$root" != *..* ]] || exit 64
[[ "$target" =~ ^[a-f0-9]{40}$ && "$mode" =~ ^(deploy|rollback)$ ]] || exit 64
root="$(realpath -e "$root")"
[[ -f "$root/.env" ]] || { echo "Missing server-only .env"; exit 65; }
app="$(cat "$(dirname "$0")/app.id")"
[[ "$app" =~ ^saveb-(api|admin|collector)$ ]] || exit 64
# All three roots should have the same parent, serializing shared database migrations.
exec 9>"$(dirname "$root")/.saveb-release.lock"
flock -w 1800 9 || exit 75
export DEPLOY_ROOT="$root"
project="$app-production"
event() { printf '%s %s %s %s\n' "$(date -u +%FT%TZ)" "$mode" "$target" "$1" >> "$root/releases.log"; }
validate() {
    [[ "$1" =~ ^[a-f0-9]{40}$ ]] || return 64
    [[ -f "$root/releases/$1/compose.yml" && "$(cat "$root/releases/$1/app.id")" == "$app" ]] || return 65
    [[ "$(cat "$root/releases/$1/image.ref")" =~ ^ghcr.io/[a-z0-9._/-]+@sha256:[a-f0-9]{64}$ ]] || return 65
}
compose() {
    local revision="$1"; shift
    RELEASE_IMAGE="$(cat "$root/releases/$revision/image.ref")" \
        docker compose --project-name "$project" --project-directory "$root" \
        --env-file "$root/.env" -f "$root/releases/$revision/compose.yml" "$@"
}
activate() {
    validate "$1" || return
    compose "$1" config --quiet || return
    docker pull "$(cat "$root/releases/$1/image.ref")" || return
    compose "$1" up -d --remove-orphans --wait --wait-timeout 180 || return
}
atomic_state() { printf '%s\n' "$2" > "$root/.$1.tmp"; mv "$root/.$1.tmp" "$root/$1"; }
validate "$target"
previous=""
[[ ! -f "$root/current" ]] || previous="$(cat "$root/current")"
if [[ -n "$previous" ]]; then validate "$previous"; fi
# A disconnected SSH session may have left a partial switch. Recover the recorded version first.
if [[ -f "$root/pending" ]]; then
    [[ -n "$previous" ]] || { echo "Unfinished first deployment: inspect services before retrying"; exit 70; }
    activate "$previous" || { event recovery_failed; exit 70; }
    rm "$root/pending"
    event recovered_previous
fi
if [[ "$mode" == rollback ]]; then
    [[ -f "$root/releases/$target/succeeded" ]] || { echo "Rollback target was never healthy"; exit 65; }
fi
compose "$target" config --quiet
docker pull "$(cat "$root/releases/$target/image.ref")"
if [[ "$mode" == deploy && -f "$root/releases/$target/migrate.required" ]]; then
    # Operator-owned hook must finish a verified backup before any schema change.
    [[ -x "$root/pre-migrate.sh" ]] || { echo "Install server-only pre-migrate.sh backup hook first"; exit 65; }
    "$root/pre-migrate.sh" "$target" || { event backup_failed; exit 1; }
    if ! compose "$target" --profile tools run --rm --no-deps migrate; then
        event migration_failed
        echo "Migration failed. Existing services left running; inspect the database before retrying."
        exit 1
    fi
fi
atomic_state pending "$target"
event activating
if activate "$target"; then
    [[ -z "$previous" ]] || atomic_state previous "$previous"
    atomic_state current "$target"
    touch "$root/releases/$target/succeeded"
    rm "$root/pending"
    event succeeded
    echo "Healthy release: $target"
else
    event activation_failed
    if [[ -n "$previous" ]] && activate "$previous"; then
        rm "$root/pending"
        event rolled_back
        echo "Deployment failed; restored previous release: $previous" >&2
    else
        event rollback_failed
        echo "Deployment failed; no healthy previous release could be restored. pending retained." >&2
    fi
    exit 1
fi
