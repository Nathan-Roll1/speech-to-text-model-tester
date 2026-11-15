#!/bin/bash
# Copy Deepgram overlay into the installed pi-web and inject the script tag.
set -euo pipefail
HOME_DIR="${HOME:-/Users/roman}"
DICTATE_JS="${HOME_DIR}/.pi/agent/bin/pi-web-dictate.js"
WEB_ROOT="/opt/homebrew/lib/node_modules/@agegr/pi-web"
[[ -f "$DICTATE_JS" && -d "$WEB_ROOT/public" ]] || exit 0
cp "$DICTATE_JS" "$WEB_ROOT/public/pi-web-dictate.js"

# Deepgram key for browser-side dictation (page is localhost + tailnet only).
# Source: ~/.pi/agent/deepgram.key first, else DEEPGRAM_API_KEY exported in a
# shell rc (the launchd context never loads those, so we scrape it here).
KEY_SRC="${HOME_DIR}/.pi/agent/deepgram.key"
KEY_VALUE=""
if [[ -s "$KEY_SRC" ]]; then
  KEY_VALUE="$(head -n 1 "$KEY_SRC" | tr -d '[:space:]')"
fi
if [[ -z "$KEY_VALUE" ]]; then
  KEY_LINE="$(grep -h '^[[:space:]]*export[[:space:]][[:space:]]*DEEPGRAM_API_KEY=' \
    "${HOME_DIR}/.zshrc" "${HOME_DIR}/.zprofile" "${HOME_DIR}/.zshenv" \
    "${HOME_DIR}/.bashrc" "${HOME_DIR}/.bash_profile" 2>/dev/null | head -n 1)"
  if [[ -n "$KEY_LINE" ]]; then
    KEY_VALUE="${KEY_LINE#*=}"                  # drop 'export DEEPGRAM_API_KEY='
    KEY_VALUE="${KEY_VALUE%%$'\r'}"             # strip CR
    KEY_VALUE="${KEY_VALUE%%[[:space:]]*}"      # strip trailing spaces/comments gap
    KEY_VALUE="${KEY_VALUE%\"}"                # strip optional quotes
    KEY_VALUE="${KEY_VALUE#\"}"
    KEY_VALUE="${KEY_VALUE%\'}"
    KEY_VALUE="${KEY_VALUE#\'}"
  fi
fi
if [[ -n "$KEY_VALUE" ]]; then
  printf '%s\n' "$KEY_VALUE" > "$WEB_ROOT/public/pi-web-dictate-key.txt"
  chmod 644 "$WEB_ROOT/public/pi-web-dictate-key.txt"
fi
HTML="$WEB_ROOT/.next/server/app/index.html"
[[ -f "$HTML" ]] || exit 0
if grep -q 'pi-web-dictate.js' "$HTML"; then
  exit 0
fi
/usr/bin/python3 - "$HTML" <<'PY'
from pathlib import Path
import sys
html = Path(sys.argv[1])
t = html.read_text()
tag = '<script src="/pi-web-dictate.js" defer></script>'
if tag in t:
    raise SystemExit(0)
i = t.rfind("</body></html>")
if i < 0:
    raise SystemExit("index.html has no </body></html>")
html.write_text(t[:i] + tag + t[i:])
PY
