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
# DSM can refuse bind mounts whose host directories do not already exist.
instagram="$root/platforms/instagram"
for directory in "$instagram" "$instagram/config" "$instagram/state" /volume1/se-p/ins-p /volume1/se-v/ins-v; do
    test ! -L "$directory" || { echo 'Instagram directories cannot be symlinks'; exit 1; }
done
mkdir -p "$instagram/config" "$instagram/state"
chmod 700 "$instagram/config" "$instagram/state"
for kind in p v; do
    directory="/volume1/se-$kind/ins-$kind"
    reference="/volume1/se-$kind/x-$kind"
    if test ! -d "$directory"; then
        mkdir -p "$directory"
        if test -d "$reference"; then
            chown "$(stat -c '%u:%g' "$reference")" "$directory"
            chmod "$(stat -c '%a' "$reference")" "$directory"
        else
            chmod 755 "$directory"
        fi
    fi
done
echo 'New v4 state and workspace prepared. Existing configuration and archives preserved.'
