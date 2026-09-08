#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
: "${DEPLOY_HOST:?missing SSH host}" "${DEPLOY_USER:?missing SSH user}"
: "${DEPLOY_SSH_KEY:?missing SSH private key}" "${DEPLOY_KNOWN_HOSTS:?missing pinned host key}"
app="$(cat deploy/app.id)"
root="${DEPLOY_ROOT:-/home/admin_chen/www/$app}"
port="${DEPLOY_PORT:-22}"
[[ "$app" =~ ^saveb-(api|admin|collector)$ ]] || exit 64
[[ "$root" =~ ^/[a-zA-Z0-9_/-]+$ && "$root" != / && "$root" != *..* ]] || exit 64
[[ "$DEPLOY_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && "$DEPLOY_HOST" =~ ^[a-zA-Z0-9][a-zA-Z0-9.-]*$ && "$port" =~ ^[0-9]{1,5}$ ]] || exit 64
tmp="$(mktemp -d)"
trap 'rm -f "$tmp/key" "$tmp/known_hosts"; rmdir "$tmp"' EXIT
printf '%s\n' "$DEPLOY_SSH_KEY" > "$tmp/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$tmp/known_hosts"
ssh_args=(-p "$port" -i "$tmp/key" -o BatchMode=yes -o ConnectTimeout=15
    -o ServerAliveInterval=30 -o ServerAliveCountMax=10
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$tmp/known_hosts")
host="$DEPLOY_USER@$DEPLOY_HOST"
revision="${ROLLBACK_SHA:-$GITHUB_SHA}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo "Full commit SHA required"; exit 64; }
if [[ -n "${ROLLBACK_SHA:-}" ]]; then
    ssh "${ssh_args[@]}" "$host" "bash '$root/releases/$revision/release.sh' '$root' '$revision' rollback"
else
    [[ "$IMAGE_NAME" =~ ^ghcr.io/[a-z0-9._/-]+$ && "$IMAGE_DIGEST" =~ ^sha256:[a-f0-9]{64}$ ]] || exit 64
    [[ "$GITHUB_RUN_ID" =~ ^[0-9]+$ && "$GITHUB_RUN_ATTEMPT" =~ ^[0-9]+$ ]] || exit 64
    printf '%s@%s\n' "$IMAGE_NAME" "$IMAGE_DIGEST" > deploy/image.ref
    inbox="$root/incoming-$revision-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"
    ssh "${ssh_args[@]}" "$host" "umask 077; mkdir -p '$root/releases'; mkdir '$inbox'"
    tar -C deploy -czf - . | ssh "${ssh_args[@]}" "$host" "tar -xzf - -C '$inbox'"
    # Never overwrite a retained release bundle on a workflow rerun.
    ssh "${ssh_args[@]}" "$host" "if test -d '$root/releases/$revision'; then cmp '$inbox/image.ref' '$root/releases/$revision/image.ref' || exit 65; else mv '$inbox' '$root/releases/$revision'; fi; bash '$root/releases/$revision/release.sh' '$root' '$revision' deploy"
fi
