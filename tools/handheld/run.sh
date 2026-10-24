#!/bin/bash
# Builds the hand-held tracker core with the synthetic scene and the replay reader into one Mac binary, then runs it.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${TMPDIR:-/tmp}/handheld-harness"
H="$ROOT/ios/RepairOS/Capture/Handheld"
swiftc -O -swift-version 5 -o "$OUT" \
  "$H/HandheldMath.swift" "$H/ObjectModel.swift" "$H/ObjectFeatures.swift" "$H/ObjectTracker.swift" "$H/RawFrames.swift" "$H/FlipAligner.swift" \
  "$ROOT/ios/RepairOSTests/HandheldSynthetic.swift" "$ROOT/tools/handheld/Replay.swift" "$ROOT/tools/handheld/main.swift"
"$OUT" "$@"
