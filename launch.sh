#!/usr/bin/env bash
# launch.sh — kick off the ingest pipeline under a fresh, capped scope.
#
# Wraps run.sh with systemd-run + memory cap. Avoids the multi-line
# copy-paste hazard of typing the full systemd-run invocation by hand:
# trailing whitespace after a backslash silently breaks fish/bash line
# continuation, and the result is an UNCAPPED run in your terminal's
# cgroup. This script is a single execution unit, so the continuations
# in the source file always apply correctly.
#
# Usage:
#   ~/Library/tools/launch.sh
#
# After launch, verify the cap is real in another terminal once Marker
# has started (~30-60s):
#   set PID (pgrep -f marker_single | head -1)
#   cat /sys/fs/cgroup(cat /proc/$PID/cgroup | cut -d: -f3)/memory.max
# Should print 8589934592 (= 8 GiB), NOT the literal string "max".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="library-$(date +%H%M)"

cat <<EOF
Launching ingest under capped scope: $UNIT
  MemoryMax     8G
  MemorySwapMax 2G
  CPUWeight     50  (half of default 100)
  IOWeight      50
  nice -n 19, ionice -c2 -n7

Verify the cap is real in another terminal once Marker starts:
  set PID (pgrep -f marker_single | head -1)
  cat /sys/fs/cgroup(cat /proc/\$PID/cgroup | cut -d: -f3)/memory.max
Should print a number like 8589934592 — NOT the literal string "max".

EOF

exec systemd-run --user --scope --unit="$UNIT" \
    -p MemoryMax=8G -p MemorySwapMax=2G \
    -p CPUWeight=50 -p IOWeight=50 \
    nice -n 19 ionice -c2 -n7 \
    "$SCRIPT_DIR/run.sh"
