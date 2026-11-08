#!/usr/bin/env bash
# Copy the canonical client/_runpod.py into every skill dir that talks to the
# worker-comfyui endpoints. Each skill keeps its own copy (self-contained and
# movable); this script is the only thing that should write those copies.
#
#   client/sync.sh          copy, and list what changed
#   client/sync.sh --check  exit 1 if any copy differs from the canonical file
#
# The DnD character-sheet skill (Multica skill dnd-character-sheet,
# 8332843f-2267-494b-a528-94b9b0233af9) also carries a copy, but it lives in
# Multica, not on disk: push it with
#   multica skill files upsert 8332843f-2267-494b-a528-94b9b0233af9 \
#     --path scripts/_runpod.py --content-file client/_runpod.py
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/_runpod.py"

TARGETS=(
  /home/stefan/studio/skills/world/generate_image/scripts/_runpod.py
  /home/stefan/studio/skills/world/flux_edit/scripts/_runpod.py
  /home/stefan/studio/skills/world/chroma_inpaint/scripts/_runpod.py
  /home/stefan/studio/skills/world/long_video/scripts/_runpod.py
  /home/stefan/hermes_assistant/.hermes/camera-kit/world/send_nsfw_image/scripts/_runpod.py
  /home/stefan/hermes_assistant/.hermes/camera-kit/world/send_video/scripts/_runpod.py
)

rc=0
for t in "${TARGETS[@]}"; do
  [ -d "$(dirname "$t")" ] || { echo "skip (no dir): $t"; continue; }
  if cmp -s "$src" "$t"; then continue; fi
  if [ "${1:-}" = "--check" ]; then echo "differs: $t"; rc=1; continue; fi
  cp "$src" "$t" && echo "updated: $t"
done
exit $rc
