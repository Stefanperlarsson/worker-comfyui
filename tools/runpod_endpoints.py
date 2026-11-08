#!/usr/bin/env python3
"""Create or update the four family endpoints on RunPod (REST API v1).

    RUNPOD_API_KEY=... tools/runpod_endpoints.py <tag-suffix> [family ...]

<tag-suffix> picks the image: ghcr.io/stefanperlarsson/worker-comfyui:<family>-<suffix>
(a short commit sha from build-family). Idempotent by name: a template/endpoint
called worker-comfyui-<family> is updated in place, otherwise created. Prints
`<family> <endpoint id>` per family -- those go in .env as RUNPOD_ENDPOINT_<FAMILY>.

No network volume: everything is baked into the image, so any datacenter can
take a worker.
"""
import json
import os
import sys

import requests

API = "https://rest.runpod.io/v1"
IMAGE = "ghcr.io/stefanperlarsson/worker-comfyui"
FAMILIES = ("moss", "flux", "chroma", "h3")

# 24 GB cards; 4090 (Ada) and 5090 (Blackwell) are both covered by the cu128 torch.
GPUS = ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090"]

# Europe only (Stefan, 2026-11-08: no US, nothing Five Eyes). Iceland, Norway, Sweden,
# Netherlands, Romania, Czechia, France. Capacity is thinner than worldwide: Romania
# carries most of the 4090/5090 stock, the rest is low or none at any moment.
DATA_CENTERS = ["EUR-IS-1", "EUR-IS-2", "EUR-IS-3", "EUR-NO-1", "EU-SE-1", "EU-NL-1",
                "EU-RO-1", "EU-CZ-1", "EU-FR-1"]

ENDPOINT = {
    "dataCenterIds": DATA_CENTERS,
    "gpuCount": 1,
    "gpuTypeIds": GPUS,
    "minCudaVersion": "12.8",
    "workersMin": 0,          # scale to zero: nothing bills while unused
    "workersMax": 1,          # one worker per family, models stay loaded in it
    "idleTimeout": 60,        # seconds a warm worker waits for the next job (billed)
    "flashboot": True,
    "scalerType": "QUEUE_DELAY",
    "scalerValue": 4,
}
EXECUTION_TIMEOUT_MS = {"h3": 7200000}  # H3 renders run up to 2 h; stills get 30 min
DEFAULT_EXECUTION_TIMEOUT_MS = 1800000

TEMPLATE_ENV = {
    "COMFY_LOG_LEVEL": "INFO",
    "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
}


def call(method, path, body=None):
    r = requests.request(method, f"{API}{path}", json=body, timeout=60,
                         headers={"Authorization": f"Bearer {os.environ['RUNPOD_API_KEY']}"})
    if r.status_code >= 400:
        sys.exit(f"{method} {path}: HTTP {r.status_code} {r.text[:500]}")
    return r.json() if r.text else {}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    suffix = sys.argv[1]
    families = sys.argv[2:] or FAMILIES
    templates = {t["name"]: t for t in call("GET", "/templates")}
    endpoints = {e["name"]: e for e in call("GET", "/endpoints")}
    for fam in families:
        name = f"worker-comfyui-{fam}"
        tpl = {
            "name": name,
            "imageName": f"{IMAGE}:{fam}-{suffix}",
            "isServerless": True,
            "containerDiskInGb": 20,
            "volumeInGb": 0,
            "env": {**TEMPLATE_ENV, "WORKER_FAMILY": fam},
        }
        if name in templates:
            tid = templates[name]["id"]
            call("PATCH", f"/templates/{tid}", {k: v for k, v in tpl.items() if k != "isServerless"})
        else:
            tid = call("POST", "/templates", tpl)["id"]
        ep = {**ENDPOINT, "name": name, "templateId": tid,
              "executionTimeoutMs": EXECUTION_TIMEOUT_MS.get(fam, DEFAULT_EXECUTION_TIMEOUT_MS)}
        if name in endpoints:
            eid = endpoints[name]["id"]
            call("PATCH", f"/endpoints/{eid}", ep)
        else:
            eid = call("POST", "/endpoints", ep)["id"]
        print(fam, eid, tpl["imageName"])


if __name__ == "__main__":
    main()
