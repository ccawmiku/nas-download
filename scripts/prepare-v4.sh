#!/bin/sh
# Prepare new state directories without changing credentials or existing media.
set -eu
umask 077
root=${NAS_DOWNLOAD_ROOT:-/volume2/docker/nas-download}
workspace=${NAS_WORKSPACE_HOST:-/volume1/NDtempwork}
test "$(id -u)" = 0 || { echo 'Run as root'; exit 1; }
test -d "$root/platforms" && test -d "$root/telegram" || { echo 'Existing v3 data root required'; exit 1; }
test ! -L "$root/v4" && test ! -L "$workspace" || { echo 'State/workspace cannot be symlinks'; exit 1; }
mkdir -p "$root/v4/core" "$root/v4/execution" "$workspace"
chmod 700 "$root/v4/core"
chown 100:101 "$root/v4/execution" "$workspace"
chmod 2775 "$root/v4/execution" "$workspace"
echo 'New v4 state and workspace prepared. Existing configuration and archives preserved.'
