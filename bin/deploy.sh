#!/usr/bin/env bash
# Deploy the pi-agent integration files from this repo to their live locations.
# Run after editing the copies in pi-agent/ — then test dictation (⌘D).
#
#   ./bin/deploy.sh          copy files + restart the musci-server LaunchAgent
#
# Live locations:
#   musci-server.py          -> ~/.pi/agent/bin/
#   dictate-webview.ts       -> ~/.pi/agent/extensions/   (needs /reload or new session)
#   com.roman.musci-server.plist -> ~/Library/LaunchAgents/
#   dictate-menubar.py           -> ~/.pi/agent/bin/  (global ⌘⇧D dictation)
#   com.roman.dictate-menubar.plist -> ~/Library/LaunchAgents/
#   pi-web-dictate.js        -> ~/.pi/agent/bin/ AND pi-web public/ (page reload picks it up)
#   pi-web-apply-dictate-overlay.sh -> ~/.pi/agent/bin/
set -euo pipefail
cd "$(dirname "$0")/.."

PI=~/.pi/agent
PUBLIC=/opt/homebrew/lib/node_modules/@agegr/pi-web/public

cp pi-agent/musci-server.py            "$PI/bin/musci-server.py"
cp pi-agent/dictate-webview.ts         "$PI/extensions/dictate-webview.ts"
cp pi-agent/com.roman.musci-server.plist ~/Library/LaunchAgents/com.roman.musci-server.plist
cp pi-agent/pi-web-dictate.js          "$PI/bin/pi-web-dictate.js"
cp pi-agent/pi-web-dictate.js          "$PUBLIC/pi-web-dictate.js"
cp pi-agent/pi-web-apply-dictate-overlay.sh "$PI/bin/pi-web-apply-dictate-overlay.sh"
chmod +x "$PI/bin/pi-web-apply-dictate-overlay.sh"
cp pi-agent/dictate-menubar.py        "$PI/bin/dictate-menubar.py"
cp pi-agent/com.roman.dictate-menubar.plist ~/Library/LaunchAgents/com.roman.dictate-menubar.plist

launchctl kickstart -k gui/$(id -u)/com.roman.musci-server
launchctl kickstart -k gui/$(id -u)/com.roman.dictate-menubar
echo "deployed. musci-server restarting (health: http://127.0.0.1:31046/health)"
echo "pi-web sessions need /reload or a new session for extension changes;"
echo "pi-web pages need a hard refresh for overlay changes."
