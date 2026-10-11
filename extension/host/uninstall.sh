#!/usr/bin/env bash
set -euo pipefail
HOST_NAME="com.antharmaya.siphon"
for base in \
  "$HOME/.config/vivaldi/NativeMessagingHosts" \
  "$HOME/.config/vivaldi-snapshot/NativeMessagingHosts" \
  "$HOME/.config/google-chrome/NativeMessagingHosts" \
  "$HOME/.config/chromium/NativeMessagingHosts" \
  "$HOME/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts" \
  "$HOME/.config/microsoft-edge/NativeMessagingHosts" \
  "$HOME/snap/vivaldi/current/.config/vivaldi/NativeMessagingHosts"; do
  f="$base/$HOST_NAME.json"
  [ -f "$f" ] && rm -v "$f"
done
echo "done"
