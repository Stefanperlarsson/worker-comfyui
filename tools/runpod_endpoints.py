#!/usr/bin/env python3
"""Create or update the family endpoints on RunPod (GraphQL).

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

IMAGE = "ghcr.io/stefanperlarsson/worker-comfyui"
FAMILIES = ("moss", "flux", "chroma", "h3", "anima")

# 24 GB cards; 4090 (Ada) and 5090 (Blackwell) are both covered by the cu128 torch.
GPUS = ["NVIDIA GeForce RTX 4090", "NVIDIA GeForce RTX 5090"]

# Netherlands, Romania, Czechia only (Stefan, 2026-11-08: no US, no Five Eyes, no
# Scandinavia, no France). Romania carries most of the 4090/5090 stock; NL and CZ are
# low or none at any moment, so expect queue waits when Romania is full.
DATA_CENTERS = ["EU-NL-1", "EU-RO-1", "EU-CZ-1"]

ENDPOINT = {
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


def gql(query):
    """RunPod GraphQL. The REST API host (rest.runpod.io) stopped resolving on
    2026-11-18 (NXDOMAIN), so everything goes through GraphQL now."""
    r = requests.post("https://api.runpod.io/graphql", json={"query": query}, timeout=60,
                      headers={"Authorization": f"Bearer {os.environ['RUNPOD_API_KEY']}"})
    data = r.json()
    if r.status_code >= 400 or data.get("errors"):
        sys.exit(f"graphql: {r.status_code} {json.dumps(data)[:500]}")
    return data["data"]


def set_locations(eid, name, tid, timeout_ms):
    """Pin the endpoint to DATA_CENTERS. REST PATCH accepts dataCenterIds but does not
    apply it (2026-11-08: version bumps, locations stay null), so this goes through
    GraphQL saveEndpoint, which needs the full endpoint input or it resets fields."""
    idpart = f'id:"{eid}", ' if eid else ""
    q = ('mutation { saveEndpoint(input:{%sname:"%s", templateId:"%s", '
         'gpuIds:"ADA_24,ADA_32_PRO", gpuCount:1, workersMin:%d, workersMax:%d, idleTimeout:%d, '
         'scalerType:"%s", scalerValue:%d, networkVolumeId:"", locations:"%s", '
         'executionTimeoutMs:%d, flashBootType:FLASHBOOT}) { id locations } }') % (
        idpart, name, tid, ENDPOINT["workersMin"], ENDPOINT["workersMax"], ENDPOINT["idleTimeout"],
        ENDPOINT["scalerType"], ENDPOINT["scalerValue"], ",".join(DATA_CENTERS), timeout_ms)
    ep = gql(q)["saveEndpoint"]
    return ep["id"], ep["locations"]


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    suffix = sys.argv[1]
    families = sys.argv[2:] or FAMILIES
    me = gql("{ myself { endpoints { id name } podTemplates { id name isServerless } } }")["myself"]
    templates = {t["name"]: t for t in me["podTemplates"] if t["isServerless"]}
    endpoints = {e["name"]: e for e in me["endpoints"]}
    for fam in families:
        name = f"worker-comfyui-{fam}"
        env = {**TEMPLATE_ENV, "WORKER_FAMILY": fam}
        envs = ", ".join('{key:"%s", value:"%s"}' % kv for kv in env.items())
        idpart = f'id:"{templates[name]["id"]}", ' if name in templates else ""
        image = f"{IMAGE}:{fam}-{suffix}"
        tid = gql('mutation { saveTemplate(input:{%sname:"%s", imageName:"%s", isServerless:true, '
                  'containerDiskInGb:20, volumeInGb:0, dockerArgs:"", env:[%s]}) { id } }'
                  % (idpart, name, image, envs))["saveTemplate"]["id"]
        eid = endpoints[name]["id"] if name in endpoints else None
        eid, locs = set_locations(eid, name, tid, EXECUTION_TIMEOUT_MS.get(fam, DEFAULT_EXECUTION_TIMEOUT_MS))
        print(fam, eid, image, locs)

if __name__ == "__main__":
    main()
