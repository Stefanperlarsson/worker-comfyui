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

Detach-safe results: pass save_to=<output path> to submit_job/poll_job and the
job is collected by a separate process in its own session, which polls RunPod
and writes the result to save_to even if the calling script is killed (an agent
turn timing out, a closed terminal). The caller just waits on that collector.
Lost a job anyway? `python3 _runpod.py fetch <family> <job_id> <output>`
recovers it while RunPod still holds the result (~30 min after it finishes).
"""

import base64
import json
import os
import signal
import subprocess
import sys
import tempfile
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


def submit_job(endpoint_id, api_key, workflow, images=None, save_to=None,
               timeout=7200, queue_timeout=3600, poll_interval=5):
    """Queue a workflow, return the job id. With save_to, a detached collector
    starts on the job right away (see start_collector) -- use this when
    submitting a batch, so every job is safe before the first one is waited on."""
    job_id = _submit(endpoint_id, api_key, workflow, images)
    if save_to:
        start_collector(endpoint_id, api_key, job_id, save_to, timeout, poll_interval, queue_timeout)
    return job_id


def _submit(endpoint_id, api_key, workflow, images=None):
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


def poll_job(endpoint_id, api_key, job_id, timeout, poll_interval, queue_timeout=3600, label="",
             save_to=None):
    """Wait for a job; return its COMPLETED status dict, raise RuntimeError /
    TimeoutError otherwise.

    With save_to: the wait is done by a detached collector (started here if
    submit_job did not already start one) that writes the output file itself,
    so the result lands on disk even if this process dies. Without it: polls
    in-process and cancels the job if this process is killed."""
    if save_to:
        start_collector(endpoint_id, api_key, job_id, save_to, timeout, poll_interval, queue_timeout)
        return _await_collector(job_id, poll_interval, label)
    return _poll_job_cancel_on_kill(endpoint_id, api_key, job_id, timeout, poll_interval,
                                    queue_timeout, label)


def _poll_job_cancel_on_kill(endpoint_id, api_key, job_id, *args, **kwargs):
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


# --- detached collection ----------------------------------------------------
# One collector process per job, in its own session (setsid), so a kill aimed at
# the caller or its process group does not reach it. Its state lives in
# STATE_DIR/<job_id>.{log,json}: the log is its Status lines (the waiting caller
# echoes them), the json its final status dict, written last. The output file
# is written atomically (.part then rename) before the json, so a json on disk
# means the file is complete.

STATE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "runpod-jobs")


def _state_paths(job_id):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        base = STATE_DIR
    except OSError:
        base = tempfile.gettempdir()
    return os.path.join(base, f"{job_id}.log"), os.path.join(base, f"{job_id}.json")


def _prune_state(base, max_age_s=2 * 86400):
    """Drop state files of collectors whose caller never came back for them."""
    cutoff = time.time() - max_age_s
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if name.endswith((".log", ".json")) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except OSError:
        pass


def start_collector(endpoint_id, api_key, job_id, save_to, timeout, poll_interval, queue_timeout):
    """Spawn the detached collector for job_id unless one already has."""
    log_path, _ = _state_paths(job_id)
    if os.path.exists(log_path):
        return
    _prune_state(os.path.dirname(log_path))
    save_to = os.path.abspath(save_to)
    env = dict(os.environ, RUNPOD_API_KEY=api_key)
    with open(log_path, "a") as log:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "collect", endpoint_id, job_id, save_to,
             str(timeout), str(poll_interval), str(queue_timeout)],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            env=env, start_new_session=True, close_fds=True)
    print(f"  [DETACH] job {job_id} -> {save_to} (collector survives this process)", flush=True)


def _await_collector(job_id, poll_interval, label=""):
    """Echo the collector's log until it writes its final status, then return
    it (COMPLETED) or raise like _poll_job does."""
    log_path, json_path = _state_paths(job_id)
    pos = 0
    while True:
        try:
            with open(log_path) as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
            for line in chunk.splitlines():
                print(f"{label}{line.lstrip()}" if label else line, flush=True)
        except FileNotFoundError:
            pass
        if os.path.exists(json_path):
            with open(json_path) as f:
                data = json.load(f)
            os.remove(json_path)
            os.remove(log_path)
            status = data.get("status")
            if status == "COMPLETED":
                return data
            if status == "TIMED_OUT_CLIENT":
                raise TimeoutError(data.get("error", "timed out"))
            raise RuntimeError(data.get("error") or f"Job {status}")
        time.sleep(min(poll_interval, 5))


def _collect(endpoint_id, job_id, save_to, timeout, poll_interval, queue_timeout):
    """The collector process body: poll, save the payload, then the status."""
    api_key = os.environ["RUNPOD_API_KEY"]
    _, json_path = _state_paths(job_id)
    try:
        data = _poll_job(endpoint_id, api_key, job_id, timeout, poll_interval, queue_timeout)
        payload = find_payload(data.get("output"))
        if payload:
            os.makedirs(os.path.dirname(save_to) or ".", exist_ok=True)
            part = save_to + ".part"
            with open(part, "wb") as f:
                f.write(fetch_payload_bytes(payload))
            os.replace(part, save_to)
            print(f"  [SAVED] {save_to}", flush=True)
        else:
            print("  [WARN] job completed but its output holds no file", flush=True)
    except TimeoutError as e:
        data = {"status": "TIMED_OUT_CLIENT", "error": str(e)}
    except Exception as e:  # FAILED/CANCELLED from _poll_job, or a save error
        data = {"status": "FAILED", "error": str(e)}
    with open(json_path + ".part", "w") as f:
        json.dump(data, f)
    os.replace(json_path + ".part", json_path)


def fetch(family, job_id, save_to):
    """Recover a finished job's file by id (RunPod keeps results ~30 min)."""
    endpoint_id, api_key = credentials(family)
    resp = requests.get(f"{API_ROOT}/{endpoint_id}/status/{job_id}",
                        headers={"Authorization": f"Bearer {api_key}"}, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") != "COMPLETED":
        sys.exit(f"job {job_id}: {data.get('status')} {data.get('error') or ''}")
    payload = find_payload(data.get("output"))
    if not payload:
        sys.exit(f"job {job_id}: completed but no file in its output")
    with open(save_to, "wb") as f:
        f.write(fetch_payload_bytes(payload))
    print(f"[COMPLETE] {os.path.abspath(save_to)}")


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


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    if cmd == ["collect"]:
        ep, jid, out, t, pi, qt = sys.argv[2:8]
        _collect(ep, jid, out, int(float(t)), float(pi), int(float(qt)))
    elif cmd == ["fetch"]:
        import _env  # noqa: F401 -- only present beside a skill's scripts
        _env.load_dotenv_fallback()
        fetch(*sys.argv[2:5])
    else:
        sys.exit("usage: _runpod.py fetch <family> <job_id> <output>")
