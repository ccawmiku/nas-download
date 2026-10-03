#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
DOCKER=${DOCKER:-/usr/local/bin/docker}
BASE=/volume2/docker
NEW=$BASE/nas-download
OLD_PLATFORMS=$BASE/nas-auto-download-integrated
OLD_TELEGRAM=$BASE/telethon-media-bot
BACKUP=$BASE/.nas-download-backups/$(date +%Y%m%d-%H%M%S)

test "$(id -u)" = 0 || { echo 'Run as root'; exit 1; }
for source in "$OLD_PLATFORMS" "$OLD_TELEGRAM"; do
  test -d "$source" && test ! -L "$source" || { echo 'Expected original data directory is missing or is a symlink'; exit 1; }
done
test ! -e "$NEW/platforms" && test ! -e "$NEW/telegram"
test -f "$NEW/compose.yaml" && test -f "$NEW/.env"
# DSM inherited ACLs require the parent to remain traversable by the bot user.
chmod 755 "$NEW"
# DSM recalculates inherited ACLs when directories move. Keep the original
# Docker parent ACL so existing container users retain session write access.
if test -x /usr/syno/bin/synoacltool; then
  /usr/syno/bin/synoacltool -enforce-inherit "$NEW" >/dev/null
fi
chmod 600 "$NEW/.env"
"$DOCKER" image inspect ghcr.io/ccawmiku/nas-download:v3.0.0 ghcr.io/ccawmiku/nas-download-telegram:v3.0.0 >/dev/null
mkdir -p "$BACKUP"
"$DOCKER" inspect nas-auto-download xhs-api telethon-media-bot > "$BACKUP/container-inspect.json"
chmod 600 "$BACKUP/container-inspect.json"
cp -a "$NEW/compose.yaml" "$NEW/.env" "$BACKUP/"
echo "$BACKUP" > "$NEW/backup-location"
download_root=$("$DOCKER" inspect telethon-media-bot --format '{{range .Mounts}}{{if eq .Destination "/downloads"}}{{.Source}}{{end}}{{end}}')

rollback() {
  code=$?
  if test "$code" -ne 0; then
    echo 'Migration failed; restoring original services'
    "$DOCKER" compose -f "$NEW/compose.yaml" --env-file "$NEW/.env" down >/dev/null 2>&1 || true
    if test -d "$NEW/platforms" && test ! -e "$OLD_PLATFORMS"; then mv "$NEW/platforms" "$OLD_PLATFORMS"; fi
    if test -d "$NEW/telegram" && test ! -e "$OLD_TELEGRAM"; then mv "$NEW/telegram" "$OLD_TELEGRAM"; fi
    "$DOCKER" start xhs-api nas-auto-download telethon-media-bot >/dev/null 2>&1 || true
  fi
}
trap rollback EXIT
"$DOCKER" stop -t 45 nas-auto-download telethon-media-bot xhs-api >/dev/null
cp -a "$OLD_PLATFORMS" "$BACKUP/platforms"
cp -a "$OLD_TELEGRAM" "$BACKUP/telegram"
mv "$OLD_PLATFORMS" "$NEW/platforms"
mv "$OLD_TELEGRAM" "$NEW/telegram"
# Verify every regular file before any new service can write to it.
test -z "$(rsync -nrc --out-format='%n' "$BACKUP/platforms/" "$NEW/platforms/")"
test -z "$(rsync -nrc --out-format='%n' "$BACKUP/telegram/" "$NEW/telegram/")"
mkdir -p "$NEW/telegram/download-root"
if [[ "$download_root" == /volume1/@docker/volumes/*/_data ]]; then
  cp -a "$download_root/." "$NEW/telegram/download-root/"
fi
"$DOCKER" compose -f "$NEW/compose.yaml" --env-file "$NEW/.env" up -d
ready=0
for attempt in $(seq 1 60); do
  if "$DOCKER" exec nas-download python -c 'import json,urllib.request; d=json.load(urllib.request.urlopen("http://127.0.0.1:14001/api/status",timeout=10)); assert len(d["services"])==5 and all(x["ready"] for x in d["services"])' >/dev/null 2>&1 &&
     "$DOCKER" exec nas-download-telegram python -c 'import json,urllib.request; assert json.load(urllib.request.urlopen("http://127.0.0.1:8000/healthz",timeout=5))["bot_running"]' >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
test "$ready" = 1
echo 'Existing platform files, Telegram configuration/history/session and media mappings migrated successfully.'
echo "Backup: $BACKUP"
date -Iseconds > "$NEW/migration-complete"
trap - EXIT
