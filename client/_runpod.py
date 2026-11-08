#!/usr/bin/env python3
"""RunPod serverless transport for the worker-comfyui family endpoints.

CANONICAL COPY: /home/stefan/worker-comfyui/client/_runpod.py. Every skill dir
that talks to RunPod carries an identical copy (so each stays self-contained and
movable); edit the canonical file, then run client/sync.sh to push it out.
Never edit a copy in place.

Each model family runs on its own endpoint so its models stay loaded between
jobs: credentials("flux") reads RUNPOD_ENDPOINT_FLUX, and so on (see FAMILIES).
Inputs travel base64 in the request's `images` list (any file type -- the
worker saves it to ComfyUI's input dir); there is no network volume.
"""

import base64
import os
import signal
import time

import requests

API_ROOT = "https://api.runpod.ai/v2"

# Keys a RunPod ComfyUI worker may hang its payload off, in search order.
# Broad on purpose: workers differ, and a video job's envelope is not the same
# shape as an image job's.
PAYLOAD_KEYS = (
    "image", "images", "video", "videos", "audio",
    "output", "data", "result", "message", "file", "files",
)


# Model family -> the env var holding its endpoint id.
FAMILIES = {
    "moss": "RUNPOD_ENDPOINT_MOSS",      # MOSS-Image text-to-image
    "flux": "RUNPOD_ENDPOINT_FLUX",      # Flux.2 Klein 9B ref edits (+ lenovo, snofs)
    "chroma": "RUNPOD_ENDPOINT_CHROMA",  # Chroma HD t2i / img2img / SAM3 inpaint
    "h3": "RUNPOD_ENDPOINT_H3",          # MiniMax H3 PRISM video (LongMedia, r2v)
}


def credentials(family):
    """(endpoint_id, api_key) for one model family's endpoint -- (None, None)
    when either is missing, which callers report as a config error rather than
    crashing. An unknown family is a programming error and raises."""
    return os.environ.get(FAMILIES[family]), os.environ.get("RUNPOD_API_KEY")


def missing_credentials_message(family):
    return f"[ERROR] {FAMILIES[family]} and RUNPOD_API_KEY must be set"


def submit_job(endpoint_id, api_key, workflow, images=None):
    url = f"{API_ROOT}/{endpoint_id}/run"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"input": {"workflow": workflow}}
    if images:
        payload["input"]["images"] = images

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    job_id = data.get("id")
    if not job_id:
        raise RuntimeError(f"No job ID in response: {data}")
    return job_id


def _poll_job(endpoint_id, api_key, job_id, timeout, poll_interval, queue_timeout=3600, label=""):
    """
    Two-clock polling: queue wait and in-progress time are tracked separately.
    `timeout` caps only IN_PROGRESS time; `queue_timeout` caps IN_QUEUE wait.
    A job stuck in queue while a parallel batch saturates the pod no longer
    eats its own processing budget.
    """
    url = f"{API_ROOT}/{endpoint_id}/status/{job_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    queue_start = time.time()
    progress_start = None

    while True:
        time.sleep(poll_interval)
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  {label}[WARN] Status check failed, retrying: {e}")
            continue

        data = resp.json()
        status = data.get("status", "").upper()
        queue_elapsed = int(time.time() - queue_start)
        progress_elapsed = int(time.time() - progress_start) if progress_start else 0
        print(f"  {label}Status: {status} (queue {queue_elapsed}s, progress {progress_elapsed}s)")

        if status == "COMPLETED":
            return data
        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            error = data.get("error", "unknown error")
            # The generic error string is useless for diagnosis -- surface the
            # worker's own account: output.details carries the failing node,
            # exception text and memory numbers; workerId names the machine.
            worker = data.get("workerId")
            if worker:
                print(f"  {label}[FAIL] workerId: {worker}")
            details = (data.get("output") or {}).get("details") or []
            for d in details:
                for line in str(d).splitlines():
                    print(f"  {label}[FAIL] {line}")
            raise RuntimeError(f"Job {status}: {error}")

        if status == "IN_PROGRESS":
            if progress_start is None:
                progress_start = time.time()
            if time.time() - progress_start > timeout:
                cancel_job(endpoint_id, api_key, job_id)
                raise TimeoutError(f"Job exceeded in-progress timeout {timeout}s")
        else:
            # IN_QUEUE or unknown — count against queue_timeout, not the
            # processing budget. The runpod handler reports IN_QUEUE while
            # the worker pool is saturated by other jobs.
            if time.time() - queue_start > queue_timeout:
                cancel_job(endpoint_id, api_key, job_id)
                raise TimeoutError(f"Job stuck in queue beyond {queue_timeout}s")


def poll_job(endpoint_id, api_key, job_id, *args, **kwargs):
    """_poll_job, plus a cancel when this process is told to die. A client
    killed mid-poll (agent run reaped, Ctrl-C, terminal closed) used to leave
    its job rendering on the worker for nobody, billing GPU until it finished
    on its own. SIGTERM/SIGHUP/SIGINT now cancel the job before exiting.
    SIGKILL cannot be caught -- nothing in-process survives that."""
    def _die(signum, frame):
        raise SystemExit(128 + signum)
    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            previous[sig] = signal.signal(sig, _die)
        except ValueError:  # not the main thread -- signals stay as they were
            pass
    try:
        return _poll_job(endpoint_id, api_key, job_id, *args, **kwargs)
    except (KeyboardInterrupt, SystemExit):
        cancel_job(endpoint_id, api_key, job_id, reason="client killed")
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def cancel_job(endpoint_id, api_key, job_id, reason="timeout"):
    """Best-effort cancel when we stop waiting. An abandoned job renders to
    completion for nobody -- the output is never fetched -- and bills GPU
    time the whole way. Cancel failure is a warning, never an error: the
    timeout is already being raised."""
    try:
        requests.post(f"{API_ROOT}/{endpoint_id}/cancel/{job_id}",
                      headers={"Authorization": f"Bearer {api_key}"}, timeout=30)
        print(f"  [CANCEL] Job {job_id} cancelled ({reason})")
    except requests.RequestException as e:
        print(f"  [WARN] Cancel failed -- job may keep billing until done: {e}")


def find_payload(output):
    """First string buried anywhere in a job's `output` -- base64 blob or URL.

    Workers wrap the interesting bytes in wildly different envelopes
    ({"images": [...]}, {"message": "..."}, a bare string), so this walks the
    structure instead of pinning one shape.
    """
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in PAYLOAD_KEYS:
            if key in output:
                result = find_payload(output[key])
                if result:
                    return result
    if isinstance(output, list) and output:
        return find_payload(output[0])
    return None


def fetch_payload_bytes(payload):
    """Raw bytes for whatever find_payload() returned: an http(s) URL is
    downloaded, anything else is treated as base64 (data: prefix stripped)."""
    if payload.startswith("http://") or payload.startswith("https://"):
        resp = requests.get(payload, timeout=300)
        resp.raise_for_status()
        return resp.content
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[-1]
    return base64.b64decode(payload)
