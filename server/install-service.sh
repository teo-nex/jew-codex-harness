#!/bin/zsh
# Install (or re-install) the Jev Router launchd service.
# Run ONCE by the user, from THEIR Terminal (launchctl is deliberately
# restricted inside supervised agents).
#
#   bash ~/Documents/Github/jev-codex-router/server/install-service.sh
#
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v /usr/local/bin/python3 || command -v python3)"
LABEL="${JEV_ROUTER_LABEL:-com.thibaultsaintjean.jev-router}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGDIR="$HOME/Library/Logs"
CODEX_HOME_VALUE="${CODEX_HOME:-$HOME/.codex}"
STATE_VALUE="${MODEL_ROUTER_STATE_DIR:-${CODEX_ROUTER_STATE_DIR:-${KIMI_CODEX_STATE_DIR:-$CODEX_HOME_VALUE/codex-router}}}"

xml_escape() {
  printf '%s' "$1" |
    sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
      -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}

CODEX_HOME_XML="$(xml_escape "$CODEX_HOME_VALUE")"
STATE_XML="$(xml_escape "$STATE_VALUE")"
JEV_ENV_XML=""
if [ -n "${JEV_ENV_FILE:-}" ]; then
  JEV_ENV_XML="$(xml_escape "$JEV_ENV_FILE")"
fi
JEV_KEY_FILE_XML=""
if [ -n "${TYPESAFE_API_KEY_FILE:-}" ]; then
  JEV_KEY_FILE_XML="$(xml_escape "$TYPESAFE_API_KEY_FILE")"
fi

[ -x "$PYTHON" ] || { echo "python3 not found"; exit 1; }
mkdir -p "$LOGDIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO/server/jev_server.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/jev-router.out.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/jev-router.err.log</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>CODEX_HOME</key><string>$CODEX_HOME_XML</string>
    <key>CODEX_ROUTER_STATE_DIR</key><string>$STATE_XML</string>
$(if [ -n "$JEV_ENV_XML" ]; then
    printf '    <key>JEV_ENV_FILE</key><string>%s</string>\n' "$JEV_ENV_XML"
  fi)
$(if [ -n "$JEV_KEY_FILE_XML" ]; then
    printf '    <key>TYPESAFE_API_KEY_FILE</key><string>%s</string>\n' "$JEV_KEY_FILE_XML"
  fi)
    <key>JEV_DISABLE_AUTO_ASTRA</key><string>${JEV_DISABLE_AUTO_ASTRA:-0}</string>
  </dict>
</dict>
</plist>
EOF

# Replace only this service's own registration.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
ready=false
attempt=0
while [ "$attempt" -lt 60 ]; do
  attempt=$((attempt + 1))
  if "$PYTHON" "$REPO/server/healthcheck.py" >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [ "$ready" = true ]; then
  "$PYTHON" "$REPO/server/healthcheck.py"
  echo ""
  echo "— Jev Router service OK ($LABEL)"
else
  echo "Jev Router health check failed" >&2
  exit 1
fi
echo "Uninstall: launchctl bootout gui/\$(id -u)/$LABEL"
