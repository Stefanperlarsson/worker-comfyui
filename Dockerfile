# Build argument for base image selection
ARG BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

# Stage 1: Base image with common dependencies
FROM ${BASE_IMAGE} AS base

# Build arguments for this stage with sensible defaults for standalone builds
ARG COMFYUI_VERSION=latest
ARG CUDA_VERSION_FOR_COMFY
# Force-install a PyTorch build with kernels for the target GPU. comfy-cli
# ignores --cuda-version inside Docker (no GPU to detect), so we pin it here.
# RunPod's Hub builds this Dockerfile with default ARGs (no build args), so the
# default must be correct here, not only in docker-bake.hcl.
#   cu128 = CUDA 12.8 build, the first with sm_120 kernels for Blackwell GPUs
#   (RTX 5090). Needs a host driver supporting CUDA 12.8 (R570+) — 50-series
#   hosts have it. cu126 lacks sm_120; cu130 needs a CUDA 13 / R580+ driver that
#   older hosts reject as "driver too old".
#
# cu130 was evaluated 2026-08-11 and REJECTED, deliberately — record it here so
# nobody re-derives it. The upside was real (ComfyUI #15419: the int8 path emits
# noise on CUDA < 13.0; NVFP4 text encoders want a CUDA 13 torch; async offload
# + pinned memory land there). The blocker is that the endpoint keeps the 24GB
# ADA_24 pool — i.e. the RTX 4090 — and cu130 needs an R580+ host driver that
# Ada hosts may not have. A worker that cannot start is worse than a slow one.
# Revisit only if the 4090 pool is ever dropped for an all-Blackwell endpoint.
ARG ENABLE_PYTORCH_UPGRADE=true
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu128

# Prevents prompts from packages asking for user input during installation
ENV DEBIAN_FRONTEND=noninteractive
# Prefer binary wheels over source distributions for faster pip installations
ENV PIP_PREFER_BINARY=1
# Ensures output from python is printed immediately to the terminal without buffering
ENV PYTHONUNBUFFERED=1
# Speed up some cmake builds
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

# Install Python, git and other necessary tools
RUN apt-get update && apt-get install -y \
    python3.10 \
    python3.10-venv \
    python3.10-dev \
    build-essential \
    g++ \
    git \
    wget \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    ffmpeg \
    libsndfile1 \
    && ln -sf /usr/bin/python3.10 /usr/bin/python \
    && ln -sf /usr/bin/pip3 /usr/bin/pip

# Clean up to reduce image size
RUN apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

# Install uv (latest) using official installer and create isolated venv
RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

# Use the virtual environment for all subsequent commands
ENV PATH="/opt/venv/bin:${PATH}"

# Install comfy-cli + dependencies needed by it to install ComfyUI
RUN uv pip install comfy-cli pip setuptools wheel

# Install ComfyUI
RUN if [ -n "${CUDA_VERSION_FOR_COMFY}" ]; then \
      /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --cuda-version "${CUDA_VERSION_FOR_COMFY}" --nvidia; \
    else \
      /usr/bin/yes | comfy --workspace /comfyui install --version "${COMFYUI_VERSION}" --nvidia; \
    fi

# Change working directory to ComfyUI
WORKDIR /comfyui

# Support for the network volume
ADD src/extra_model_paths.yaml ./

# Go back to the root
WORKDIR /

# Install Python runtime dependencies for the handler
RUN uv pip install runpod requests websocket-client \
    # Image & Core
    opencv-python-headless \
    scipy \
    imageio \
    scikit-image \
    pandas \
    matplotlib \
    piexif \
    numexpr \
    dill \
    \
    # AI & Models
    ultralytics \
    segment-anything \
    scikit-learn \
    einops \
    transformers \
    onnxruntime-gpu \
    accelerate \
    kornia \
    spandrel \
    \
    # GGUF / LLM / Tokenizers
    tiktoken \
    simpleeval \
    sentencepiece \
    gguf \
    \
    # WAS Node Suite & Crystools
    faker \
    pilgram \
    beautifulsoup4 \
    nvidia-ml-py \
    psutil \
    \
    # Face & Audio
    insightface \
    facexlib \
    pydub \
    edge-tts \
    librosa \
    soundfile \
    \
    # [NEW] ComfyUI-Manager Dependencies
    comfyui-workflow-templates \
    matrix-client

# Upgrade PyTorch AFTER all other deps so nothing can overwrite it
# Don't pin torch to a single version: torchvision/torchaudio hard-require an
# exact matching torch, so pinning torch alone (with the others unpinned) yields
# an unresolvable conflict whenever a newer torchvision is published. Let the
# three resolve together as a matched set from the cu128 index.
RUN if [ "$ENABLE_PYTORCH_UPGRADE" = "true" ]; then \
      uv pip install --force-reinstall torch torchvision torchaudio --index-url ${PYTORCH_INDEX_URL}; \
    fi

# Bake ComfyUI's runtime deps into /opt/venv at BUILD time so cold starts don't
# reinstall them. comfy-cli installs ComfyUI into /comfyui/.venv, but start.sh
# launches ComfyUI (from the network volume) with /opt/venv's python — so mirror
# ComfyUI's core requirements into /opt/venv. Covers alembic / SQLAlchemy
# (ComfyUI's sqlite DB) and the rest of core deps. torch is stripped so the
# cu128 build installed above is preserved.
RUN grep -ivE '^[[:space:]]*(torch|torchvision|torchaudio)([[:space:]=<>!~;].*)?$' /comfyui/requirements.txt > /tmp/comfy-reqs.txt \
    && uv pip install -r /tmp/comfy-reqs.txt

# Custom-node deps the volume's ComfyUI imports that aren't covered above (keeps
# PuLID / RMBG / face-parsing / fill-nodes / mmaudio etc. loading), plus the
# transformers/huggingface-hub pin from upstream #227 (transformers 5.x /
# huggingface-hub 1.x break ComfyUI at startup). Heavy compiled deps
# (llama-cpp-python, groundingdino) are still intentionally omitted — add them
# here only if a workflow needs those specific nodes. sageattention is NO LONGER
# omitted: it is compiled in its own stage below and installed into `final`.
#
# color-matcher and mss are ComfyUI-KJNodes' two requirements that nothing else
# here pulls in. They are pure-Python and tiny, and without them KJNodes fails to
# import — which would take `PathchSageAttentionKJ` down with it, i.e. the node
# that actually switches SageAttention on. Keep them.
RUN uv pip install timm iopath hydra-core fal-client torchdiffeq blend_modes \
    color-matcher mss \
    "transformers>=4.50.3,<5" "huggingface-hub<1.0"

# ComfyUI's own first-party runtime packages.
#
# THIS BLOCK IS A RECURRING FAILURE POINT — read before touching. The mirror
# step above installs the IMAGE's /comfyui/requirements.txt, but the ComfyUI
# that actually RUNS lives on the network volume and moves independently. Every
# time the volume's ComfyUI gains a new first-party dependency, this image is
# behind and the worker dies. It has now happened twice:
#   comfy-kitchen  — missing: every quantized checkpoint died at load with
#                    "'NoneType' object has no attribute 'Params'" (get_layout_class()
#                    returning None from quant_ops.py's ImportError fallback).
#   comfy-aimdo    — missing (2026-08-12): ComfyUI would not BOOT at all.
#                    comfy/memory_management.py line 7 does `import
#                    comfy_aimdo.host_buffer`, so main.py raised ModuleNotFoundError
#                    and the handler then failed 600 connection attempts to :8188.
#                    Every job on the endpoint failed, not just some.
#
# Versions must track https://github.com/comfyanonymous/ComfyUI/blob/master/requirements.txt
# — NOT the image's copy, which is what misled us both times. When a worker dies
# on a missing/mismatched comfy-* module, diff that file against this block first.
#
# NOTE the ordering trap: this RUN comes AFTER the requirements mirror, so an
# EXACT pin here DOWNGRADES whatever the mirror resolved. The old
# comfy-kitchen==0.2.26 pin was actively pulling the image backwards from the
# 0.2.30 that ComfyUI now requires.
RUN uv pip install "comfy-kitchen==0.2.30" "comfy-aimdo==0.4.13" comfy-angle

# [CRITICAL FIX] Force-create the directory that ComfyUI Manager crashes on
RUN mkdir -p /opt/venv/lib/python3.10/site-packages/comfyui_workflow_templates/templates
RUN touch /opt/venv/lib/python3.10/site-packages/comfyui_workflow_templates/templates/.keep
    
# Add application code and scripts
ADD src/start.sh src/network_volume.py handler.py test_input.json ./
RUN chmod +x /start.sh

# Add script to install custom nodes
COPY scripts/comfy-node-install.sh /usr/local/bin/comfy-node-install
RUN chmod +x /usr/local/bin/comfy-node-install

# Prevent pip from asking for confirmation during uninstall steps in custom nodes
ENV PIP_NO_INPUT=1

# Copy helper script to switch Manager network mode at container start
COPY scripts/comfy-manager-set-mode.sh /usr/local/bin/comfy-manager-set-mode
RUN chmod +x /usr/local/bin/comfy-manager-set-mode

# Debug: print PyTorch CUDA version at build time
RUN python -c "import torch; print('TORCH VERSION:', torch.__version__); print('TORCH CUDA:', torch.version.cuda)"

# Set the default command to run when starting the container
CMD ["/start.sh"]

# Stage 1b: Compile SageAttention
#
# Why a separate stage: `base` is a CUDA *runtime* image and has no nvcc, and
# SageAttention has no usable Linux wheel on PyPI — `pip install sageattention`
# builds from source. Building it here, in a stage that is thrown away, keeps
# the ~3GB CUDA toolkit out of the shipped image; only the finished wheel is
# copied into `final`.
#
# Why FROM base: the wheel is a torch C++/CUDA extension and is ABI-tied to the
# exact torch build it was compiled against. Deriving from `base` means it is
# compiled against the very same /opt/venv torch that will import it at runtime.
# A standalone builder stage would have to re-resolve torch and could silently
# drift (torch is deliberately unpinned above), producing a wheel that imports
# and then segfaults.
FROM base AS sageattention-builder

ARG SAGEATTENTION_REF=v2.2.0
# 8.9 = Ada (RTX 4090), 12.0 = Blackwell (RTX 5090, RTX PRO Blackwell).
#
# THE `+PTX` IS LOAD-BEARING. DO NOT REMOVE IT. (2026-08-11, after a live
# failure.) setup.py only emits the PTX fallback gencode line for an arch that
# carries a `+PTX` suffix; without it you get `-gencode arch=compute_X,code=sm_X`
# and nothing else, so the wheel runs on EXACTLY the listed archs and hard-fails
# everywhere else with:
#     CUDA error: no kernel image is available for execution on the device
# raised from SamplerCustomAdvanced, mid-render, after the job has already paid
# for queue and model load. Because a serverless endpoint schedules across a
# whole GPU pool, that presents as an INTERMITTENT failure that depends on which
# card you land on -- some renders succeed, some die. The PTX blob lets the
# driver JIT for any newer architecture (sm_121 and up) instead of dying.
#
# Do not drop 8.9 while any Ada card is in the endpoint's GPU list.
# NOTE: setup.py hard-fails if nvcc is older than 12.8 while 12.0 is listed, so
# the toolkit below is pinned rather than inherited from the base image.
ARG SAGE_CUDA_ARCH_LIST="8.9 12.0+PTX"
# Kept deliberately modest: nvcc is memory-hungry and a CI builder that OOMs
# halfway through wastes the entire compile. Raise on a big machine.
ARG SAGE_MAX_JOBS=4
ARG SAGE_EXT_PARALLEL=2
ARG SAGE_NVCC_THREADS=4

RUN wget -qO /tmp/cuda-keyring.deb \
      https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends cuda-toolkit-12-8 \
    && rm -rf /var/lib/apt/lists/* /tmp/cuda-keyring.deb

# Must match the CUDA the torch in /opt/venv was built against (cu128), or the
# extension compiles and then fails to load.
ENV CUDA_HOME=/usr/local/cuda-12.8
ENV PATH="${CUDA_HOME}/bin:${PATH}"

# setup.py reads TORCH_CUDA_ARCH_LIST and skips GPU autodetection entirely when
# it is set — that is what makes this compilable on a builder with no GPU.
RUN git clone --depth 1 --branch "${SAGEATTENTION_REF}" \
      https://github.com/thu-ml/SageAttention.git /tmp/sageattention \
    && cd /tmp/sageattention \
    && TORCH_CUDA_ARCH_LIST="${SAGE_CUDA_ARCH_LIST}" \
       MAX_JOBS="${SAGE_MAX_JOBS}" \
       EXT_PARALLEL="${SAGE_EXT_PARALLEL}" \
       NVCC_APPEND_FLAGS="--threads ${SAGE_NVCC_THREADS}" \
       python setup.py bdist_wheel \
    && mkdir -p /wheels \
    && cp dist/*.whl /wheels/ \
    && ls -lh /wheels/

# Stage 2: Download models
FROM base AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
# Set default model type if none is provided
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI
WORKDIR /runpod-volume/runpod-slim/ComfyUI/

# Stage 3: Final image
FROM base AS final

# SageAttention, compiled in stage 1b. This is the stage RunPod builds when no
# --target is given (it is the last one in the file), so this is where the wheel
# has to land. If you ever build `base` directly, it will NOT have SageAttention.
#
# The import check is deliberately non-fatal: the builder has no GPU, and some
# sageattention versions probe CUDA at import time. A failure here means "could
# not verify", not "broken" — read the build log rather than trusting silence.
COPY --from=sageattention-builder /wheels /tmp/wheels
RUN uv pip install /tmp/wheels/*.whl \
    && rm -rf /tmp/wheels \
    && (python -c "import sageattention; print('SAGEATTENTION: import OK')" \
        || echo "SAGEATTENTION: installed, import unverified (no GPU at build time)")

# Copy models from stage 2 to the final image
# COPY --from=downloader /comfyui/models /comfyui/models
