#!/usr/bin/env bash
#
# Turns a terminal that has been in service into a clean master for cloning.
#
#   bash deploy/linux/make-master.sh t1
#
# Why this exists. A terminal installed from any older package downloads its
# build update on the first login - about 190 MB - and that download starves
# the deal-history fetch, so that first sync fails with "no deal history
# arrived" while reporting the balance perfectly correctly. Every clone of
# that master repeats it.
#
# A terminal that has already run has both halves solved: it downloaded the
# update and applied it on its next start, and it has accumulated the broker
# list. Promoting it to master means clones are born current and download
# nothing. Measured: a clone of a current master logged in and synchronised
# in two seconds with zero LiveUpdate activity.
#
# What has to come out of it first is the point of this script. A working
# terminal stores the credentials of every account it has ever logged into
# in Config/accounts.dat. That file must never travel with a master.

set -euo pipefail

SOURCE="${1:-}"
PREFIX="${WINEPREFIX:-/opt/mt5/wine}"
MT5_DIR="${MT5_DIR:-$PREFIX/drive_c/MT5}"
DEST="${2:-$MT5_DIR/master}"

[ -n "$SOURCE" ] || { echo "usage: $0 <instance-name> [dest]   e.g. $0 t1" >&2; exit 1; }
SRC_DIR="$MT5_DIR/$SOURCE"
[ -f "$SRC_DIR/terminal64.exe" ] || { echo "no terminal at $SRC_DIR" >&2; exit 1; }

if pgrep -f "$SOURCE\\\\terminal64.exe" >/dev/null 2>&1; then
  echo "$SOURCE is running. Stop the service first: systemctl stop mt5api" >&2
  exit 1
fi

STAGE="$MT5_DIR/.master-staging.$$"
rm -rf "$STAGE"
cp -a "$SRC_DIR" "$STAGE"

# Config is capitalised on a portable install. Match it either way rather
# than silently leaving the credentials behind.
CONFIG="$(find "$STAGE" -maxdepth 1 -iname config | head -1)"
[ -n "$CONFIG" ] || { echo "no config directory in $SRC_DIR" >&2; rm -rf "$STAGE"; exit 1; }

rm -f "$CONFIG/accounts.dat" "$CONFIG/dnsperf.dat"
rm -rf "$STAGE/logs" "$STAGE/Bases" "$STAGE/Tester" "$STAGE/MQL5/Logs"
mkdir -p "$STAGE/Bases"

if find "$STAGE" -iname accounts.dat | grep -q .; then
  echo "refusing to publish a master that still carries accounts.dat" >&2
  rm -rf "$STAGE"; exit 1
fi

if [ -d "$DEST" ]; then
  BACKUP="$DEST.replaced-$(date +%Y%m%d-%H%M%S)"
  mv "$DEST" "$BACKUP"
  echo "previous master kept at $BACKUP"
fi
mv "$STAGE" "$DEST"

SERVERS="$(find "$DEST" -iname servers.dat | head -1)"
cat <<EOF

Master ready at $DEST
  terminal64.exe  $(stat -c %s "$DEST/terminal64.exe") bytes
  servers.dat     $([ -n "$SERVERS" ] && stat -c %s "$SERVERS" || echo missing) bytes
  credentials     removed
  size            $(du -sh "$DEST" | cut -f1)

Keep this directory as the deployment artefact. A new box clones from it and
needs no warm-up at all. Refresh it the same way whenever the build moves on.
EOF
