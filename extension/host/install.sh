#!/usr/bin/env bash
# Installs the siphon native-messaging host for Vivaldi / Chrome / Chromium / Edge / Brave and for
# Firefox / Zen / LibreWolf. Most people want `siphon setup browser` instead; this one points the
# browsers at the host in this checkout, for working on it.
# Re-run after moving the repo. Idempotent.
set -euo pipefail

HOST_NAME="com.antharmaya.siphon"
EXT_ID="pbeimeldfnbenghllloohnnofbfaicfe"    # GitHub / unpacked build (key in manifest.json)
STORE_ID="dkicnkfpoodpflagdafjoobaoclfndlp"  # Chrome Web Store build (the store assigns its own ID)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOST_PY="$HERE/siphon_host.py"
chmod +x "$HOST_PY"

read -r -d '' MANIFEST <<JSON || true
{
  "name": "$HOST_NAME",
  "description": "siphon video downloader native host",
  "path": "$HOST_PY",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXT_ID/", "chrome-extension://$STORE_ID/"]
}
JSON

# Every Chromium-family browser that might be installed, Linux paths.
DIRS=(
  "$HOME/.config/vivaldi/NativeMessagingHosts"
  "$HOME/.config/vivaldi-snapshot/NativeMessagingHosts"
  "$HOME/.config/google-chrome/NativeMessagingHosts"
  "$HOME/.config/chromium/NativeMessagingHosts"
  "$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts"
  "$HOME/.config/microsoft-edge/NativeMessagingHosts"
  "$HOME/snap/vivaldi/current/.config/vivaldi/NativeMessagingHosts"
)

# Firefox-family browsers: their own dirs, and the add-on ID instead of an origin.
FIREFOX_ID="siphon@antharmaya.com"
read -r -d '' FF_MANIFEST <<JSON || true
{
  "name": "$HOST_NAME",
  "description": "siphon video downloader native host",
  "path": "$HOST_PY",
  "type": "stdio",
  "allowed_extensions": ["$FIREFOX_ID"]
}
JSON

installed=0
for d in .mozilla .zen .librewolf .floorp .waterfox; do
  if [ -d "$HOME/$d" ]; then
    mkdir -p "$HOME/$d/native-messaging-hosts"
    printf '%s\n' "$FF_MANIFEST" > "$HOME/$d/native-messaging-hosts/$HOST_NAME.json"
    echo "installed -> $HOME/$d/native-messaging-hosts/$HOST_NAME.json"
    installed=$((installed+1))
  fi
done

for base in "${DIRS[@]}"; do
  parent="$(dirname "$base")"
  # Only install where the browser's own config dir already exists (don't create stray dirs).
  if [ -d "$parent" ] || [ -d "${parent%/NativeMessagingHosts}" ]; then
    mkdir -p "$base"
    printf '%s\n' "$MANIFEST" > "$base/$HOST_NAME.json"
    echo "installed -> $base/$HOST_NAME.json"
    installed=$((installed+1))
  fi
done

if [ "$installed" -eq 0 ]; then
  echo "no browser config found; nothing installed" >&2
  exit 1
fi
echo
echo "Host installed for extension ids $EXT_ID (GitHub) and $STORE_ID (Chrome Web Store)"
echo "Now load the extension (vivaldi://extensions -> Load unpacked -> $(dirname "$HERE"))"
