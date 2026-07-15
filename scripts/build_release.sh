#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

python3 -m pip install -q -r requirements.txt -r requirements-build.txt
rm -rf build dist
python3 -m PyInstaller --noconfirm CountdownApp.spec

OUT="dist/VT Vocal Countdown"
ARCH="$(uname -m)"
ZIP="dist/VT-Vocal-Countdown-macOS-${ARCH}.zip"
rm -f "$ZIP"
ditto -c -k --sequesterRsrc --keepParent "$OUT" "$ZIP"
echo "Built $ZIP"
ls -lh "$ZIP"
