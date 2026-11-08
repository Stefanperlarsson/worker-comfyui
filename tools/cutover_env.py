#!/usr/bin/env python3
"""Point the skills' .env files at the family endpoints.

    tools/cutover_env.py moss=<id> flux=<id> chroma=<id> h3=<id> [--apply]

For each .env below: sets RUNPOD_ENDPOINT_<FAMILY>=<id> (only the families that
file's skills use), and comments out INPUT_S3_BUCKET so refs go base64 instead
of being staged to the old volume, which the new endpoints cannot see.
RUNPOD_ENDPOINT_ID (the old volume endpoint) is left alone -- retired scripts
still read it until that endpoint is deleted. Without --apply it only prints the
plan. Each file is backed up beside itself as <name>.bak-serverless-split first.
"""
import shutil
import sys

ENV_FILES = {
    # studio skills: generate_image (moss, chroma), flux_edit, chroma_inpaint, long_video (h3)
    "/home/stefan/studio/.env": ("moss", "flux", "chroma", "h3"),
    # DnD character sheet: chroma + flux
    "/home/stefan/sillytavern/.env": ("flux", "chroma"),
    # Hermes camera-kit (send_nsfw_image = flux, send_video = h3): the profiles of the three
    # live characters that run the kit ($HERMES_HOME/.env is read first). The shared
    # .hermes/.env is left alone.
    "/home/stefan/hermes_assistant/.hermes/profiles/dahiana/.env": ("flux", "h3"),
    "/home/stefan/hermes_assistant/.hermes/profiles/shanice/.env": ("flux", "h3"),
    "/home/stefan/hermes_assistant/.hermes/profiles/navneet/.env": ("flux", "h3"),
}


def plan(path, families, ids):
    lines = open(path).read().splitlines()
    want = {f"RUNPOD_ENDPOINT_{f.upper()}": ids[f] for f in families}
    out, seen, changes = [], set(), []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in want:
            seen.add(key)
            new = f"{key}={want[key]}"
            if line != new:
                changes.append(f"set {key}")
            out.append(new)
        elif key == "INPUT_S3_BUCKET":
            out.append("# " + line + "  # off since the 2026-11-08 serverless split: refs go base64")
            changes.append("comment out INPUT_S3_BUCKET")
        else:
            out.append(line)
    missing = [k for k in want if k not in seen]
    if missing:
        out.append("")
        out.append("# worker-comfyui family endpoints (2026-11-08): one endpoint per model family")
        for k in missing:
            out.append(f"{k}={want[k]}")
            changes.append(f"add {k}")
    return "\n".join(out) + "\n", changes


def main():
    apply = "--apply" in sys.argv
    ids = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
    for path, families in ENV_FILES.items():
        need = [f for f in families if f not in ids]
        if need:
            sys.exit(f"missing ids for {need}")
        text, changes = plan(path, families, ids)
        print(f"{path}: {', '.join(changes) or 'no change'}")
        if apply and changes:
            shutil.copy2(path, path + ".bak-serverless-split")
            with open(path, "w") as f:
                f.write(text)


if __name__ == "__main__":
    main()
