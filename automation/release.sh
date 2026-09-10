#!/usr/bin/env bash
# 发布锁覆盖三个仓库；只切换应用镜像，不重置业务库或修改服务器私有配置。
set -Eeuo pipefail
umask 077
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
app="$(cat "$script_dir/app.id")"
[[ "$app" =~ ^saveb-(api|admin|collector)$ ]] || exit 64
www="$(realpath -e "${SAVEB_WWW_ROOT:-/home/admin_chen/www}")"
root="$(realpath -e "${1:?project directory required}")"
target="${2:?full commit SHA required}"
mode="${3:-deploy}"
image="${4:-}"
[[ "$root" == "$www/$app" && "$target" =~ ^[a-f0-9]{40}$ && "$mode" =~ ^(deploy|rollback)$ ]] || exit 64
[[ -f "$root/.env" && -f "$root/.deploy-ready" ]] || {
    echo 'Prepare .env, RBAC/business data and attachments, then create .deploy-ready first.' >&2; exit 65;
}
exec 9>"$www/.saveb-release.lock"
flock -w 1800 9 || exit 75
project="$app-production"
mkdir -p "$root/releases"
event() { printf '%s %s %s %s\n' "$(date -u +%FT%TZ)" "$mode" "$target" "$1" >> "$root/releases.log"; }
atomic_state() { printf '%s\n' "$2" > "$root/.$1.tmp"; mv "$root/.$1.tmp" "$root/$1"; }
validate() {
    [[ "$1" =~ ^[a-f0-9]{40}$ ]] || return 64
    [[ -f "$root/releases/$1/compose.yml" && "$(cat "$root/releases/$1/app.id")" == "$app" ]] || return 65
    [[ "$(cat "$root/releases/$1/image.ref")" =~ ^ghcr.io/ding-cykj/$app@sha256:[a-f0-9]{64}$ ]] || return 65
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
    compose "$1" up -d --no-build --pull never --wait --wait-timeout 300 || return
}
if [[ "$mode" == deploy ]]; then
    [[ "$image" =~ ^ghcr.io/ding-cykj/$app@sha256:[a-f0-9]{64}$ ]] || exit 64
    compose_file=docker-compose.server.yml
    [[ "$app" != saveb-admin ]] || compose_file=docker-compose.yml
    source_compose="$script_dir/../$compose_file"
    # 相同 SHA 不得被重新构建出的不同镜像覆盖，以保证回滚记录可信。
    if [[ -d "$root/releases/$target" ]]; then
        validate "$target"
        [[ "$(cat "$root/releases/$target/image.ref")" == "$image" ]] || { echo 'SHA already recorded with a different digest; make a new commit.' >&2; exit 65; }
        cmp -s "$source_compose" "$root/releases/$target/compose.yml" || exit 65
    else
        stage="$(mktemp -d "$root/incoming-XXXXXX")"
        trap '[[ -z "${stage:-}" || ! -d "$stage" ]] || rm -rf -- "$stage"' EXIT
        cp "$source_compose" "$stage/compose.yml"
        cp "$script_dir/app.id" "$stage/app.id"
        printf '%s\n' "$image" > "$stage/image.ref"
        mv "$stage" "$root/releases/$target"
        stage=''
    fi
fi
validate "$target"
previous=''
[[ ! -f "$root/current" ]] || previous="$(cat "$root/current")"
[[ -z "$previous" ]] || validate "$previous"
# 中断或断电后的首次重试先恢复已确认健康版本；首次发布中断须人工检查。
if [[ -f "$root/pending" ]]; then
    [[ -n "$previous" ]] || { echo 'Unfinished first deployment; inspect services and pending before retrying.' >&2; exit 70; }
    activate "$previous" || { event recovery_failed; exit 70; }
    rm "$root/pending"
    event recovered_previous
fi
if [[ "$mode" == rollback ]]; then
    [[ -f "$root/releases/$target/succeeded" ]] || { echo 'Rollback target was never healthy.' >&2; exit 65; }
fi
compose "$target" config --quiet
docker pull "$(cat "$root/releases/$target/image.ref")"
if [[ "$mode" == deploy && "$app" != saveb-admin ]]; then
    bash "$script_dir/backup.sh" "$www" "$app" "$target" || { event backup_failed; exit 1; }
    if ! compose "$target" --profile tools run --rm --no-deps migrate; then
        event migration_failed
        echo 'Migration failed; inspect database. Existing services were not switched; schema is not automatically reversed.' >&2
        exit 1
    fi
fi
atomic_state pending "$target"
event activating
if activate "$target"; then
    [[ -z "$previous" || "$previous" == "$target" ]] || atomic_state previous "$previous"
    atomic_state current "$target"
    touch "$root/releases/$target/succeeded"
    rm "$root/pending"
    event succeeded
    echo "Healthy release: $app $target"
else
    event activation_failed
    if [[ -n "$previous" ]] && activate "$previous"; then
        rm "$root/pending"
        event rolled_back
        echo "Deployment failed; previous application restored: $previous" >&2
    else
        event rollback_failed
        echo 'Deployment and recovery failed; pending retained for inspection.' >&2
    fi
    exit 1
fi
