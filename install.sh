#!/bin/sh
# siphon installer — Linux and macOS, no sudo.
#
#   curl -fsSL https://raw.githubusercontent.com/antharmaya/siphon/master/install.sh | sh
#
# 1. installs uv (Astral's Python toolchain) into ~/.local/bin if you don't have it
# 2. installs siphon as an isolated tool (uv brings a Python if yours is too old)
# 3. runs `siphon setup -y`: fetches ffmpeg / a JS runtime only if this machine lacks them
#
# Env knobs:  SIPHON_REPO (git URL)  SIPHON_EXTRAS=impersonate  SIPHON_SKIP_SETUP=1
set -eu

REPO="${SIPHON_REPO:-https://github.com/antharmaya/siphon}"
EXTRAS="${SIPHON_EXTRAS:-}"

say()  { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) warn "This script is for Linux/macOS. On Windows (PowerShell):"
     warn "  irm https://astral.sh/uv/install.ps1 | iex"
     warn "  uv tool install \"antharmaya-siphon @ git+$REPO\"; siphon setup -y"
     exit 1 ;;
esac

if ! command -v uv >/dev/null 2>&1; then
  say "→ installing uv (user-space Python toolchain)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && PATH="$d:$PATH" && export PATH
  done
  command -v uv >/dev/null 2>&1 || { warn "uv installed but not on PATH; open a new shell and re-run"; exit 1; }
fi

# Install from the repo, not PyPI, until the PyPI name is published by us: an unclaimed name
# on PyPI is something anyone could register, and this script would happily run it.
spec="antharmaya-siphon"
[ -n "$EXTRAS" ] && spec="antharmaya-siphon[$EXTRAS]"
say "→ installing siphon from $REPO"
uv tool install --force "$spec @ git+$REPO"

BIN_DIR="$(uv tool dir --bin)"
SIPHON="$BIN_DIR/siphon"
[ -x "$SIPHON" ] || { warn "install finished but $SIPHON is missing"; exit 1; }

if [ -z "${SIPHON_SKIP_SETUP:-}" ]; then
  say "→ fetching any missing tools (ffmpeg, JS runtime)"
  "$SIPHON" setup -y || warn "some tools didn't fetch; run 'siphon doctor' to see what and why"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "add $BIN_DIR to your PATH (or run: uv tool update-shell), then open a new shell" ;;
esac

say "✓ siphon $("$SIPHON" --version | cut -d' ' -f2) installed"
echo "  try:   siphon 'https://www.youtube.com/watch?v=jNQXAC9IVRw'"
echo "  check: siphon doctor"
