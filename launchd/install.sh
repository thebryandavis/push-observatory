#!/bin/bash
# Install the Push Observatory launchd jobs.
#
#   ./launchd/install.sh                 install capture + recap
#   ./launchd/install.sh --avd Pixel_7_API_34   also install the emulator job
#   ./launchd/install.sh --uninstall     remove all three
#
# Templates are copied to ~/Library/LaunchAgents with __PROJECT_DIR__,
# __HOME__ and __AVD_NAME__ substituted. Nothing is installed system-wide and
# no sudo is required.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$HERE/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LABELS=(com.pushobs.capture com.pushobs.recap com.pushobs.emulator)
AVD=""
UNINSTALL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --avd) AVD="${2:-}"; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if [[ $UNINSTALL -eq 1 ]]; then
  for l in "${LABELS[@]}"; do
    launchctl bootout "gui/$UID/$l" 2>/dev/null || true
    rm -f "$AGENTS/$l.plist"
    echo "removed $l"
  done
  exit 0
fi

mkdir -p "$AGENTS" "$PROJECT_DIR/logs"

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "ERROR: $PROJECT_DIR/.venv/bin/python not found." >&2
  echo "Create the venv first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

install_one() {
  local label="$1"
  local src="$HERE/$label.plist"
  local dst="$AGENTS/$label.plist"

  sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
      -e "s|__HOME__|$HOME|g" \
      -e "s|__AVD_NAME__|$AVD|g" \
      "$src" > "$dst"

  if grep -q '__[A-Z_]*__' "$dst"; then
    echo "ERROR: unsubstituted placeholder left in $dst:" >&2
    grep -o '__[A-Z_]*__' "$dst" | sort -u >&2
    rm -f "$dst"
    exit 1
  fi
  plutil -lint "$dst" >/dev/null

  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$dst"
  echo "installed $label"
}

install_one com.pushobs.capture
install_one com.pushobs.recap

if [[ -n "$AVD" ]]; then
  install_one com.pushobs.emulator
else
  echo
  echo "Emulator job NOT installed (no --avd given)."
  echo "Boot the AVD by hand first, complete Google sign-in and app installs,"
  echo "then re-run:  ./launchd/install.sh --avd <YourAvdName>"
fi

echo
echo "Status:"
launchctl list | grep pushobs || echo "  (nothing running yet - give it a few seconds)"
echo
echo "Logs:      tail -f $PROJECT_DIR/logs/capture.log"
echo "Stop all:  $HERE/install.sh --uninstall"
