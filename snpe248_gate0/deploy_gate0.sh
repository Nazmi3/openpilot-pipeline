#!/usr/bin/env bash
# Stage + build + run the SNPE 2.48 probe on the comma two.
#
# ROLLBACK CONTRACT -- everything this script does lives in ONE directory:
#   $DEV_DIR (default /data/tmp/snpe248)
# It does not write anywhere else, does not modify /data/openpilot, does not
# touch any init/env file, and sets LD_LIBRARY_PATH only for its own commands.
# To undo completely:  ./rollback_gate0.sh
set -euo pipefail

DEV=${DEV:-comma@172.20.10.2}
PORT=${PORT:-8022}
DEV_DIR=${DEV_DIR:-/data/tmp/snpe248}
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$PORT" "$DEV")

echo ">>> checking device reachable + free space"
"${SSH[@]}" "mkdir -p $DEV_DIR && df -h /data | tail -1"

echo ">>> uploading libs + headers + probe (~48 MB, one directory)"
tar czf - -C "$HERE" lib include snpe_probe.cc \
  | "${SSH[@]}" "tar xzf - -C $DEV_DIR"

echo ">>> building probe on device"
# libc++_shared.so is reused from openpilot's existing 1.x tree READ-ONLY --
# we never write to third_party/snpe.
"${SSH[@]}" "cd $DEV_DIR && clang++ -std=c++14 -fPIC snpe_probe.cc \
    -Iinclude -Llib -lSNPE -o snpe_probe 2>&1 | tail -20"

echo
echo ">>> GATE 0: runtime availability"
"${SSH[@]}" "cd $DEV_DIR && LD_LIBRARY_PATH=$DEV_DIR/lib:/data/openpilot/third_party/snpe/aarch64 \
    ADSP_LIBRARY_PATH=$DEV_DIR/lib ./snpe_probe" || echo "(probe exited non-zero -- see result line above)"

echo
echo ">>> GATE 1: load the existing 2.48 .dlc (read-only, not modified)"
"${SSH[@]}" "cd $DEV_DIR && LD_LIBRARY_PATH=$DEV_DIR/lib:/data/openpilot/third_party/snpe/aarch64 \
    ./snpe_probe /data/openpilot/models/supercombo.dlc" || echo "(probe exited non-zero -- see result line above)"

echo
echo "Done. Nothing outside $DEV_DIR was written. Undo with ./rollback_gate0.sh"
