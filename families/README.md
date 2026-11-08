# Model families

One image per family, all built from the same Dockerfile (`--build-arg FAMILY=<name>`).
Each family runs on its own RunPod serverless endpoint, so its models stay loaded
between jobs and are never swapped out for another family's.

Per family:

- `models.txt` — one model per line, a path under ComfyUI's `models/`
  (`unet/moss_image_sft_int8_convrot.safetensors`). The file is baked into the image at
  `/comfyui/models/<path>`. Source: the same path under `runpod-slim/ComfyUI/models/`
  on the old network volume's S3 API — needed only for a first models build.
- `nodes.txt` — custom nodes, `<git url> <commit>` per line. Empty file = core nodes only.
- `patches/<node dir>/...` — files copied over a cloned custom node after checkout.

Weights live in `ghcr.io/stefanperlarsson/worker-comfyui:models-<family>`, a layers-only
image (one layer per model file). The runnable image `:<family>` is the family's code
image with those layers appended by `tools/imgtool` — rebuilding code never re-uploads
weights. See `.github/workflows/build-family.yml`.
