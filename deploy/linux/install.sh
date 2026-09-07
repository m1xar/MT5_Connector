#!/usr/bin/env bash
#
# Brings a bare Ubuntu 24.04 box to the point where the service can run.
# Everything it creates lives under one directory - /opt/mt5 by default, or the
# parent of $WINEPREFIX - so the whole install can be removed with one rm.
# Idempotent: safe to re-run on a box that is already set up.
#
#   sudo bash deploy/linux/install.sh
#
# It does not start the service and does not create the terminal pool.
# Those are deploy/linux/clone-pool.sh and deploy/linux/service.sh.

set -euo pipefail

PREFIX="${WINEPREFIX:-/opt/mt5/wine}"
APP_DIR="$PREFIX/drive_c/app"
PY_VERSION="${PY_VERSION:-3.12.10}"
DISPLAY_NUM="${DISPLAY_NUM:-99}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

log "APT packages"
export DEBIAN_FRONTEND=noninteractive
dpkg --add-architecture i386
if [ ! -f /etc/apt/keyrings/winehq-archive.key ]; then
  mkdir -pm755 /etc/apt/keyrings
  wget -qO /etc/apt/keyrings/winehq-archive.key https://dl.winehq.org/wine-builds/winehq.key
fi
CODENAME="$(. /etc/os-release && echo "$VERSION_CODENAME")"
if [ ! -f "/etc/apt/sources.list.d/winehq-${CODENAME}.sources" ]; then
  wget -qNP /etc/apt/sources.list.d/ \
    "https://dl.winehq.org/wine-builds/ubuntu/dists/${CODENAME}/winehq-${CODENAME}.sources"
fi
apt-get update -qq
apt-get install -y -qq --install-recommends winehq-stable
apt-get install -y -qq xvfb unzip curl wget python3 python3-venv

wine --version

# Wine wants a display even to start a terminal that nobody looks at.
log "Virtual display :$DISPLAY_NUM"
export DISPLAY=":$DISPLAY_NUM"
XVFB_PID=""
if ! xdpyinfo -display ":$DISPLAY_NUM" >/dev/null 2>&1; then
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
  Xvfb ":$DISPLAY_NUM" -screen 0 1280x1024x24 -nolisten tcp >/tmp/xvfb-install.log 2>&1 &
  XVFB_PID=$!
  sleep 3
fi
# The display started here is only for this install; service.sh owns the real
# one as a systemd unit, and a leftover Xvfb would keep that unit from binding.
trap '[ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null; rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"' EXIT

log "Wine prefix at $PREFIX"
export WINEPREFIX="$PREFIX"
export WINEARCH=win64
export WINEDEBUG=-all
# Without this, wineboot blocks forever on the Mono/Gecko install dialog,
# which nothing can answer on a headless box. This is the single most
# common way a Wine setup appears to hang.
export WINEDLLOVERRIDES="mscoree,mshtml="
if [ ! -f "$PREFIX/system.reg" ]; then
  wineboot --init
  wineserver -w
fi

log "Windows build of Python $PY_VERSION inside the prefix"
# The service imports MetaTrader5, which only exists as a Windows wheel, so
# the interpreter running it has to be a Windows one - a Linux python3 cannot
# load it no matter what Wine is installed.
if ! wine 'C:\Python312\python.exe' -V >/dev/null 2>&1; then
  mkdir -p "$(dirname "$PREFIX")/downloads"
  INSTALLER="$(dirname "$PREFIX")/downloads/python-${PY_VERSION}-amd64.exe"
  [ -f "$INSTALLER" ] || curl -sSLo "$INSTALLER" \
    "https://www.python.org/ftp/python/${PY_VERSION}/python-${PY_VERSION}-amd64.exe"
  wine "$INSTALLER" /quiet InstallAllUsers=1 PrependPath=1 Include_test=0 \
    Include_launcher=0 'TargetDir=C:\Python312'
  wineserver -w
fi
wine 'C:\Python312\python.exe' -V

log "Application code into $APP_DIR"
mkdir -p "$APP_DIR"
# .env is deliberately excluded: it is per-box, and copying a developer's
# over a running server's would repoint it at the wrong database.
tar -C "$REPO_ROOT" \
    --exclude=.git --exclude=.venv --exclude=__pycache__ \
    --exclude='*.log' --exclude=.env \
    -cf - . | tar -C "$APP_DIR" -xf -

log "Python dependencies"
wine 'C:\Python312\python.exe' -m pip install --upgrade pip -q
wine 'C:\Python312\python.exe' -m pip install -q -r 'C:\app\requirements.txt'
wine 'C:\Python312\python.exe' -c \
  "import MetaTrader5, fastapi, psycopg, uvicorn; print('imports ok, MetaTrader5', MetaTrader5.__version__)"

cat <<EOF

Done. Next:

  1. Put a master terminal at $PREFIX/drive_c/MT5/master
     (a portable install, already updated to the current build).
  2. bash deploy/linux/clone-pool.sh 12
  3. Write $APP_DIR/.env  (see .env.example; paths look like C:\\MT5\\t1\\terminal64.exe)
  4. bash deploy/linux/service.sh install

EOF
