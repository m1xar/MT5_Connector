#!/usr/bin/env bash
#
# Builds the terminal pool from the master.
#
#   bash deploy/linux/clone-pool.sh 12
#
# Sizing: measured on a 6-core box, going from 6 terminals to 12 cut a
# 63-account sync storm from 218 s to 157 s, while 18 and 24 bought nothing
# and only cost memory (24 left under 1 GB free). Extra terminals pay only
# while they fill the gaps where a terminal waits on a broker; past roughly
# two per core they just split the same CPU.
#
# Clones inherit whatever build the master is on, so a current master is what
# makes a fresh pool work on its first sync. See make-master.sh.

set -euo pipefail

COUNT="${1:-12}"
PREFIX="${WINEPREFIX:-/opt/mt5/wine}"
MT5_DIR="${MT5_DIR:-$PREFIX/drive_c/MT5}"

log() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ -f "$MT5_DIR/master/terminal64.exe" ] || {
  echo "no master terminal at $MT5_DIR/master" >&2; exit 1; }

if find "$MT5_DIR/master" -iname accounts.dat | grep -q .; then
  echo "refusing to clone: the master carries Config/accounts.dat, which holds" >&2
  echo "the credentials of every account it has logged into. Rebuild it with" >&2
  echo "deploy/linux/make-master.sh before cloning." >&2
  exit 1
fi

if [ -n "$(find "$MT5_DIR/master/Bases" -mindepth 1 -maxdepth 1 -type d 2>/dev/null)" ]; then
  echo "note: the master carries per-broker caches under Bases/. Harmless, but"
  echo "      make-master.sh strips them and makes every clone smaller."
fi

CORES="$(nproc)"
if [ "$COUNT" -gt $((CORES * 2)) ]; then
  echo "warning: $COUNT terminals on $CORES cores is past the point where" >&2
  echo "         more terminals stopped helping in measurement." >&2
fi

log "Cloning master into $COUNT instances"
for i in $(seq 1 "$COUNT"); do
  if [ -d "$MT5_DIR/t$i" ]; then
    echo "  t$i exists, leaving it"
    continue
  fi
  cp -a "$MT5_DIR/master" "$MT5_DIR/t$i"
  # Caches are per-instance and must not be inherited from the master.
  rm -rf "$MT5_DIR/t$i/Bases/Default/History" \
         "$MT5_DIR/t$i/Bases/Default/ticks" \
         "$MT5_DIR/t$i/logs" 2>/dev/null || true
  echo "  t$i created"
done

du -sh "$MT5_DIR" | awk '{print "\npool on disk: " $1}'

PATHS=""
for i in $(seq 1 "$COUNT"); do PATHS="${PATHS}C:\\MT5\\t$i\\terminal64.exe;"; done

cat <<EOF

Put this in the service .env:

MT5_API_TERMINAL_PATHS=${PATHS%;}
MT5_API_TERMINAL_PORTABLE=true

Then start the service and check it:

  sudo bash deploy/linux/service.sh install
  python3 deploy/linux/verify-pool.py --token "\$MT5_API_API_TOKEN" --parallel $COUNT
EOF
