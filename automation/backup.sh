#!/usr/bin/env bash
# 新业务库一致性备份；读取现有 infra 配置，不停止数据库，不接触旧 ERP。
set -Eeuo pipefail
umask 077
www="$(realpath -e "${1:?www required}")"
app="${2:?app required}"
revision="${3:?SHA required}"
[[ "$app" =~ ^saveb-(api|collector)$ && "$revision" =~ ^[a-f0-9]{40}$ ]] || exit 64
api="$www/saveb-api"
[[ -f "$api/.env" && -f "$api/docker-compose.infra.yml" ]] || exit 65
compose=(docker compose --project-name saveb-infra --project-directory "$api" --env-file "$api/.env" -f "$api/docker-compose.infra.yml")
# 初次空库不能通过自动 migrate 代替 RBAC 与旧业务数据准备。
ready="$("${compose[@]}" exec -T postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "SELECT count(*) FROM public.users"')"
[[ "$ready" =~ ^[0-9]+$ && "$ready" -gt 0 ]] || { echo 'Target RBAC is not prepared.' >&2; exit 65; }
mkdir -p "$www/backups"
dir="$(mktemp -d "$www/backups/release-$app-${revision:0:12}-XXXXXX")"
"${compose[@]}" exec -T postgres sh -c \
 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$dir/database.dump"
test -s "$dir/database.dump"
"${compose[@]}" exec -T postgres pg_restore --list < "$dir/database.dump" > "$dir/archive.list"
(cd "$dir" && sha256sum database.dump > SHA256SUMS)
echo "Database backup and archive check passed: $dir"
