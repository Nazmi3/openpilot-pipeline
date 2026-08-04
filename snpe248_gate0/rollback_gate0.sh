#!/usr/bin/env bash
# Undo everything deploy_gate0.sh did. Gate 0/1 write exactly one directory,
# so this is the whole rollback -- there is no other state to restore.
set -euo pipefail

DEV=${DEV:-comma@172.20.10.2}
PORT=${PORT:-8022}
DEV_DIR=${DEV_DIR:-/data/tmp/snpe248}
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -p "$PORT" "$DEV")

echo ">>> removing $DEV_DIR"
"${SSH[@]}" "rm -rf $DEV_DIR && echo removed"

echo ">>> verifying openpilot untouched"
"${SSH[@]}" "cd /data/openpilot && git status --porcelain | head -20; \
  echo '--- models/ ---'; ls -la models/"
echo
echo "If git status printed nothing above, /data/openpilot is byte-identical to before."
