#!/usr/bin/env bash

# Use libtcmalloc for better memory management
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1)"
export LD_PRELOAD="${TCMALLOC}"

# Ensure ComfyUI-Manager runs in offline network mode inside the container
comfy-manager-set-mode offline || echo "worker-comfyui - Could not set ComfyUI-Manager network_mode" >&2

echo "worker-comfyui: Starting ComfyUI"

# Allow operators to tweak verbosity; default is DEBUG.
: "${COMFY_LOG_LEVEL:=DEBUG}"

# ---------------------------------------------------------------------------
# Sync the network-volume ComfyUI's Python deps into the launch venv (/opt/venv).
# The actual ComfyUI lives on the network volume and is updated independently of
# this image, so it can require newer deps (alembic/sqlalchemy for ComfyUI's
# sqlite DB, comfy_aimdo, custom-node deps) than the image was built with.
# Without this, main.py crashes on import and the API server never binds to 8188.
# torch/torchvision/torchaudio are stripped so a pinned requirement cannot
# overwrite the CUDA build baked into the image.
# ---------------------------------------------------------------------------
COMFY_DIR="/runpod-volume/runpod-slim/ComfyUI"
export VIRTUAL_ENV=/opt/venv
if command -v uv >/dev/null 2>&1; then
    INSTALL="uv pip install"
else
    INSTALL="/opt/venv/bin/python -m pip install"
fi
strip_torch() { grep -ivE '^[[:space:]]*(torch|torchvision|torchaudio)([[:space:]=<>!~;].*)?$' "$1" 2>/dev/null; }

if [ -d "$COMFY_DIR" ]; then
    echo "worker-comfyui: Syncing ComfyUI deps from volume into /opt/venv ..."
    if [ -f "$COMFY_DIR/requirements.txt" ]; then
        strip_torch "$COMFY_DIR/requirements.txt" > /tmp/_comfy_core_reqs.txt
        $INSTALL -r /tmp/_comfy_core_reqs.txt || echo "worker-comfyui: WARN core dep sync reported errors (continuing)" >&2
    fi
    for req in "$COMFY_DIR"/custom_nodes/*/requirements.txt; do
        [ -f "$req" ] || continue
        strip_torch "$req" > /tmp/_comfy_cn_reqs.txt
        $INSTALL -r /tmp/_comfy_cn_reqs.txt || echo "worker-comfyui: WARN dep sync skipped $req (continuing)" >&2
    done
    echo "worker-comfyui: ComfyUI dep sync done"
else
    echo "worker-comfyui: WARN $COMFY_DIR not found — is the network volume attached?" >&2
fi

# Serve the API and don't shutdown the container
if [ "$SERVE_API_LOCALLY" == "true" ]; then
    python -u /runpod-volume/runpod-slim/ComfyUI/main.py --disable-auto-launch --disable-metadata --listen --verbose "${COMFY_LOG_LEVEL}" --log-stdout &

    echo "worker-comfyui: Starting RunPod Handler"
    python -u /handler.py --rp_serve_api --rp_api_host=0.0.0.0
else
    python -u /runpod-volume/runpod-slim/ComfyUI/main.py --disable-auto-launch --disable-metadata --verbose "${COMFY_LOG_LEVEL}" --log-stdout &

    echo "worker-comfyui: Starting RunPod Handler"
    python -u /handler.py
fi
