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
RUN if [ "$ENABLE_PYTORCH_UPGRADE" = "true" ]; then \
      uv pip install --force-reinstall torch==2.12.0 torchvision torchaudio --index-url ${PYTORCH_INDEX_URL}; \
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
# (llama-cpp-python, sageattention, groundingdino) are intentionally omitted —
# add them here only if a workflow needs those specific nodes.
RUN uv pip install timm iopath hydra-core fal-client torchdiffeq blend_modes \
    "transformers>=4.50.3,<5" "huggingface-hub<1.0"

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

# Stage 2: Download models
FROM base AS downloader

ARG HUGGINGFACE_ACCESS_TOKEN
# Set default model type if none is provided
ARG MODEL_TYPE=flux1-dev-fp8

# Change working directory to ComfyUI
WORKDIR /runpod-volume/runpod-slim/ComfyUI/

# Stage 3: Final image
FROM base AS final

# Copy models from stage 2 to the final image
# COPY --from=downloader /comfyui/models /comfyui/models
