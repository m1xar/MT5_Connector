#!/usr/bin/env bash
#
# Installs the two systemd units the service needs and starts it.
#
#   sudo bash deploy/linux/service.sh install
#   sudo bash deploy/linux/service.sh restart
#   sudo bash deploy/linux/service.sh logs
#
# There are two units, not one: the terminals need an X display to start at
# all, so Xvfb is its own service that mt5api depends on. A unit for the
# display that already exists is left alone - another service on the box may
# own it. The launch command lives in a runner script because systemd strips
# backslashes out of ExecStart, which mangles every Windows path. systemd
# appends to the log itself, so logrotate copytruncates it rather than moving
# it; one account once wrote 1.7 GB of warnings into it in three days.
#
# The host's own upgrades are switched off here. unattended-upgrades once
# had needrestart bounce xvfb after a glib update, which took the service
# down with it and set off a pending terminal update on every clone.

set -euo pipefail

ACTION="${1:-install}"
PREFIX="${WINEPREFIX:-/opt/mt5/wine}"
APP_DIR="$PREFIX/drive_c/app"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
ROOT="$(dirname "$PREFIX")"
RUNNER="$ROOT/bin/mt5api-run.sh"
LOG="$ROOT/logs/mt5api.log"

case "$ACTION" in
  logs)    exec tail -f "$LOG" ;;
  restart) systemctl restart mt5api; sleep 5; systemctl is-active mt5api ;;
  status)  systemctl status xvfb mt5api --no-pager -l ;;
  install) ;;
  *) echo "usage: $0 install|restart|status|logs" >&2; exit 1 ;;
esac
[ "$ACTION" = install ] || exit 0

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$APP_DIR/.env" ] || { echo "no .env at $APP_DIR/.env" >&2; exit 1; }

mkdir -p "$ROOT/bin" "$ROOT/logs"

cat > /etc/logrotate.d/mt5api <<EOF
$LOG {
    daily
    maxsize 200M
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF

cat > "$RUNNER" <<EOF
#!/bin/bash
export WINEPREFIX="$PREFIX"
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="
export DISPLAY=":$DISPLAY_NUM"
export PYTHONUNBUFFERED=1
cd "$APP_DIR"
exec wine 'C:\Python312\python.exe' 'C:\app\main.py'
EOF
chmod +x "$RUNNER"

[ -f /etc/systemd/system/xvfb.service ] || cat > /etc/systemd/system/xvfb.service <<EOF
[Unit]
Description=Xvfb virtual display :$DISPLAY_NUM
After=network.target

[Service]
ExecStartPre=/bin/rm -f /tmp/.X$DISPLAY_NUM-lock /tmp/.X11-unix/X$DISPLAY_NUM
ExecStart=/usr/bin/Xvfb :$DISPLAY_NUM -screen 0 1280x1024x24 -nolisten tcp
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/mt5api.service <<EOF
[Unit]
Description=MT5 Sync API (Wine)
After=network.target postgresql.service docker.service xvfb.service
Requires=xvfb.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$RUNNER
Restart=always
RestartSec=10
TimeoutStopSec=120
KillMode=mixed
StandardOutput=append:$LOG
StandardError=append:$LOG

[Install]
WantedBy=multi-user.target
EOF

mkdir -p /etc/needrestart/conf.d
cat > /etc/needrestart/conf.d/mt5api.conf <<'EOF'
$nrconf{override_rc}{qr(^xvfb)} = 0;
$nrconf{override_rc}{qr(^mt5api)} = 0;
EOF
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
EOF
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null 2>&1 || true

systemctl daemon-reload
systemctl enable --now xvfb
systemctl enable --now mt5api

echo "waiting for the pool to come up ..."
for _ in $(seq 1 40); do
  if grep -q '"event": "pool.started"' "$LOG" 2>/dev/null; then break; fi
  sleep 5
done
grep '"event": "pool.started"' "$LOG" | tail -1 || echo "pool has not reported yet, check $LOG"
systemctl is-active mt5api
