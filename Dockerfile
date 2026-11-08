# worker-comfyui — one Dockerfile, one image per model family.
#
#   docker build --build-arg FAMILY=h3 .      (moss | flux | chroma | h3)
#
# This builds the family's CODE image: CUDA, Python, a pinned ComfyUI, the family's
# custom nodes, the handler. No weights. The weights are appended afterwards as
# prebuilt layers (tools/imgtool, .github/workflows/build-family.yml), so a code
# rebuild never re-downloads or re-uploads 40 GB of models. Nothing is read from a
# network volume any more: ComfyUI, nodes and models all live in the image.

ARG BASE_IMAGE=nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

FROM ${BASE_IMAGE} AS base

ARG FAMILY
# ComfyUI is pinned, never "latest": an unpinned rebuild pulling a newer core is
# what broke the worker twice (see git history). v0.43.1 is the release the old
# volume ran, the first known-good one for MOSS-Image. Bump deliberately.
ARG COMFYUI_REF=v0.43.1
# PyTorch from the cu128 index: the first build with sm_120 kernels (RTX 5090) that
# still runs on R570 Ada (4090) hosts. cu130 needs R580+ and was rejected 2026-08-11.
# Pinned to the exact set the old image resolved, which the SageAttention wheel
# below was built against — a different torch makes that wheel fail to load.
ARG PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
ARG TORCH_PINS="torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0"

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_PREFER_BINARY=1
ENV PYTHONUNBUFFERED=1
ENV CMAKE_BUILD_PARALLEL_LEVEL=8

RUN test -n "${FAMILY}" || (echo "FAMILY build arg is required (moss|flux|chroma|h3)" >&2; exit 1)

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
    && apt-get autoremove -y && apt-get clean -y && rm -rf /var/lib/apt/lists/*

RUN wget -qO- https://astral.sh/uv/install.sh | sh \
    && ln -s /root/.local/bin/uv /usr/local/bin/uv \
    && ln -s /root/.local/bin/uvx /usr/local/bin/uvx \
    && uv venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

RUN uv pip install pip setuptools wheel

# PyTorch first, from its own index; everything after resolves against it.
RUN uv pip install ${TORCH_PINS} --index-url ${PYTORCH_INDEX_URL}

# ComfyUI at the pinned tag. Its requirements.txt is now the one that actually runs,
# so ComfyUI's first-party packages (comfy-kitchen, comfy-aimdo, frontend...) come
# from it at the right versions — no hand-maintained pin block to fall behind.
RUN git clone https://github.com/Comfy-Org/ComfyUI /comfyui \
    && cd /comfyui && git checkout --detach "${COMFYUI_REF}" \
    && grep -ivE '^[[:space:]]*(torch|torchvision|torchaudio)([[:space:]=<>!~;].*)?$' requirements.txt > /tmp/comfy-reqs.txt \
    && uv pip install -r /tmp/comfy-reqs.txt \
    && rm -rf .git

# Handler deps, plus the transformers / huggingface-hub ceiling from upstream #227
# (transformers 5.x / huggingface-hub 1.x break ComfyUI at startup).
RUN uv pip install runpod requests websocket-client \
    opencv-python-headless scipy einops kornia spandrel \
    "transformers>=4.50.3,<5" "huggingface-hub<1.0"

# The family's custom nodes, pinned by commit, then its patches on top.
COPY families/${FAMILY} /family
RUN set -e; cd /comfyui/custom_nodes; \
    while read -r url ref; do \
      [ -z "${url}" ] && continue; \
      dir="$(basename "${url}" .git)"; \
      git clone "${url}" "${dir}"; \
      git -C "${dir}" checkout --detach "${ref}"; \
      rm -rf "${dir}/.git"; \
      if [ -f "${dir}/requirements.txt" ]; then uv pip install -r "${dir}/requirements.txt"; fi; \
    done < /family/nodes.txt; \
    if [ -d /family/patches ]; then cp -rv /family/patches/. /comfyui/custom_nodes/; fi
# Node requirements must not move torch off the pinned set.
RUN uv pip install ${TORCH_PINS} --index-url ${PYTORCH_INDEX_URL}

ADD src/start.sh src/network_volume.py handler.py test_input.json ./
RUN chmod +x /start.sh

ENV PIP_NO_INPUT=1
ENV WORKER_FAMILY=${FAMILY}

RUN python -c "import torch; print('TORCH VERSION:', torch.__version__); print('TORCH CUDA:', torch.version.cuda)"

CMD ["/start.sh"]

FROM base AS final

# SageAttention — downloaded, not compiled (compiling blew the 30-min Hub build cap).
# Built by the build-sage-wheel workflow for sm_89 + sm_120 + PTX against torch
# 2.11.0+cu128. Re-run that workflow (then rebuild) whenever TORCH_PINS change.
# An "undefined symbol" at import means the wheel and torch disagree.
ARG SAGE_WHEEL_URL=https://github.com/Stefanperlarsson/worker-comfyui/releases/download/sage-wheel-v2.2.0/sageattention-2.2.0-cp310-cp310-linux_x86_64.whl
RUN uv pip install "${SAGE_WHEEL_URL}" \
    && (python -c "import sageattention; print('SAGEATTENTION: import OK')" \
        || echo "SAGEATTENTION: installed, import unverified (no GPU at build time)")
