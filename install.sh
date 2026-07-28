#!/bin/bash
# Install the weather-bench collector as a launchd agent so snapshots keep
# accruing regardless of any Claude Code session, terminal, or login shell.
#
# macOS blocks launchd-started jobs from reading ~/Documents, so the scripts and
# database live under ~/Library/Application Support/weather-bench. This repo
# stays the source of truth for CODE; re-run this script after editing it.
#
# Usage:  ./install.sh
# Uninstall:
#   launchctl bootout gui/$UID/com.weatherbench.collect
#   rm ~/Library/LaunchAgents/com.weatherbench.collect.plist
#   (data is left in place; delete ~/Library/Application\ Support/weather-bench to remove it)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/Library/Application Support/weather-bench"
DATA_DIR="$APP_DIR/data"
PLIST="$HOME/Library/LaunchAgents/com.weatherbench.collect.plist"
LOG="$HOME/Library/Logs/weather-bench.log"
LABEL="com.weatherbench.collect"
PYTHON=/usr/bin/python3

echo "==> installing to $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR" "$HOME/Library/Logs"

# Code: always overwrite from the repo.
cp "$REPO"/common.py "$REPO"/collect.py "$REPO"/score.py "$REPO"/retro.py "$APP_DIR/"

# Data: move the existing database across on first install only. Never clobber
# an already-migrated database - that would discard accrued snapshots.
if [ -f "$REPO/data/bench.sqlite" ] && [ ! -f "$DATA_DIR/bench.sqlite" ]; then
  echo "==> migrating database (this is a move, not a copy)"
  # checkpoint WAL first so no committed rows are stranded in the sidecar files
  sqlite3 "$REPO/data/bench.sqlite" "PRAGMA wal_checkpoint(TRUNCATE);" >/dev/null
  mv "$REPO/data/bench.sqlite" "$DATA_DIR/bench.sqlite"
  rm -f "$REPO/data/bench.sqlite-wal" "$REPO/data/bench.sqlite-shm"
elif [ -f "$DATA_DIR/bench.sqlite" ]; then
  echo "==> database already migrated, leaving it untouched"
fi

echo "==> writing $PLIST"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>

    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON</string>
        <string>$APP_DIR/collect.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$APP_DIR</string>

    <!-- Every 5 hours, NOT 6. The models issue new runs on a 6-hourly cycle
         (00/06/12/18 UTC); sampling on that same period would phase-lock us to a
         fixed point in every model's update cycle forever, systematically
         catching them at a constant staleness while Foreca (which refreshes far
         more often) stays near-fresh. 5 does not divide 6, so successive samples
         precess through all six phases of the cycle within ~30 h.
         launchd also runs a missed interval as soon as the Mac wakes, which cron
         would silently skip. -->
    <key>StartInterval</key>
    <integer>18000</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
        <key>WEATHERBENCH_DATA</key>
        <string>$DATA_DIR</string>
    </dict>
</dict>
</plist>
PLIST_EOF

echo "==> (re)loading agent"
launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST"
launchctl kickstart -k "gui/$UID/$LABEL"

echo "==> installed. log: $LOG"
echo "    status:  launchctl print gui/$UID/$LABEL | head -20"
echo "    run now: launchctl kickstart -k gui/$UID/$LABEL"
