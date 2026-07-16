"""
runpod_lib
==========

Shared RunPod plumbing used by both the training launcher and the test-video
launcher. Everything RunPod-specific that isn't training-specific lives here:

  * Configuration (volume, GPU list, image, SSH key, API key, W&B defaults)
  * Pod lifecycle (create-with-GPU-fallback, wait-for-SSH, terminate)
  * SSH helpers (`ssh_run`, `download_from_pod`, `upload_file_to_pod`,
    `sync_local_files_to_pod`)
  * A log-tailing helper for `--wait` streaming

The training launcher owns its own training-specific bash script (patches,
GT generation, `.pth -> .onnx -> .dlc`); the test-video launcher owns its
own bash for running the prediction-video test. Both go through the shared
helpers below so that pod bring-up, GPU fallback, file sync and download
behave identically.

Design notes
------------
* We deliberately DON'T bootstrap the volume from this file. The training
  launcher already does that idempotently; the test launcher assumes a
  provisioned volume (conda env `optrain`, cloned pipeline repo, dataset).
  It fails loudly with an instructive error if the volume looks empty.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Iterable


# ---------------------------- CONFIG (env-overridable) ----------------------------
VOLUME_ID   = os.environ.get("RUNPOD_VOLUME_ID", "z4zbwcmpuv")   # comma2k19 vol
DATA_CENTER = os.environ.get("RUNPOD_DC", "EUR-IS-1")            # must match the volume
GPU_TYPE    = os.environ.get("RUNPOD_GPU", "NVIDIA RTX 4000 Ada Generation")

# All comfortably fit this small model (~6GB). Tried in order in the volume's DC
# until one has availability. Cheaper first.
GPU_CANDIDATES = [
    "NVIDIA RTX 4000 Ada Generation",
    "NVIDIA RTX 2000 Ada Generation",
    "NVIDIA RTX A4000",
    "NVIDIA RTX 4000 SFF Ada Generation",
    "NVIDIA RTX A4500",
    "NVIDIA RTX PRO 4500 Blackwell",
    "NVIDIA L4",
    "NVIDIA RTX A5000",
    "NVIDIA RTX 5000 Ada Generation",
    "NVIDIA A40",
    "NVIDIA L40S",
    "NVIDIA L40",
    "NVIDIA RTX A6000",
    "NVIDIA RTX 6000 Ada Generation",
    "NVIDIA GeForce RTX 4090",
    "NVIDIA A100 80GB PCIe",
]

IMAGE             = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
CONTAINER_DISK_GB = 30
MOUNT_PATH        = "/workspace"

WANDB_ENTITY  = os.environ.get("WANDB_ENTITY",  "nazmiryuki")
WANDB_PROJECT = os.environ.get("WANDB_PROJECT", "openpilot-pipeline")

SSH_KEY = os.path.expanduser(os.environ.get("RUNPOD_SSH_KEY", "~/.ssh/id_ed25519"))
API_KEY = os.environ.get("RUNPOD_API_KEY")
WANDB_KEY = os.environ.get("WANDB_API_KEY", "")


# ---------------------------- errors / SDK bring-up ----------------------------
def die(msg: str) -> None:
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(1)


def get_runpod():
    """Import and configure the runpod SDK; die with a clear error if unset."""
    try:
        import runpod  # type: ignore
    except ImportError:
        die("pip install runpod   (and: pip install requests)")
    if not API_KEY:
        die("set RUNPOD_API_KEY env var (RunPod Console -> Settings -> API Keys)")
    runpod.api_key = API_KEY
    return runpod


def list_resources() -> None:
    """Print available GPU types and the caller's network volumes."""
    runpod = get_runpod()
    print("\n=== GPU types ===")
    try:
        for g in runpod.get_gpus():
            print(f"  {g.get('id')}")
    except Exception as e:
        print("  (could not list GPUs:", e, ")")
    print("\n=== Your network volumes (use the id as --volume) ===")
    try:
        import requests  # type: ignore
        r = requests.get("https://rest.runpod.io/v1/networkvolumes",
                         headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
        r.raise_for_status()
        for v in r.json() or []:
            print(f"  id={v.get('id')}  name={v.get('name')!r}  "
                  f"dc={v.get('dataCenterId')}  size={v.get('size')}GB")
    except Exception as e:
        print("  (REST list failed:", e, ") -> RunPod Console -> Storage -> volume -> ID")
    print()


# ---------------------------- SSH plumbing ----------------------------
def _ssh_base(host: str, port: int) -> list:
    """Common ssh flags: batch mode, no host-key noise for ephemeral pods."""
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
        "-i", SSH_KEY, "-p", str(port), f"root@{host}",
    ]


def ssh_run(host, port, command, stdin=None, quiet=False, check=True):
    """Run one command over SSH. stdin bytes are LF-normalized so scripts
    execute under Linux bash regardless of this file's (Windows) line endings.
    Returns the CompletedProcess, or None if check=True and the command failed."""
    cmd = _ssh_base(host, port) + [command]
    inp = None
    if stdin is not None:
        inp = stdin.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    res = subprocess.run(cmd, input=inp, capture_output=True)
    out = res.stdout.decode("utf-8", "replace")
    err = res.stderr.decode("utf-8", "replace")
    if not quiet:
        if out.strip():
            print(out.strip())
        if err.strip() and "Permanently added" not in err:
            print(err.strip(), file=sys.stderr)
    if check and res.returncode != 0:
        return None
    return res


def wait_for_ssh(runpod, pod_id, timeout=120):
    """Block until the pod exposes SSH on a public port and responds. Return
    (host, port) on success, or None on timeout so the caller can scrap this
    host and try a fresh one."""
    print(f"Waiting for the pod to boot and expose SSH (healthy boot ~20-60s, giving it {timeout}s)...")
    start = time.time()
    deadline = start + timeout
    last_note = 0
    while time.time() < deadline:
        try:
            pod = runpod.get_pod(pod_id)
        except Exception:
            pod = None
        for p in ((pod or {}).get("runtime") or {}).get("ports") or []:
            if p.get("privatePort") == 22 and p.get("isIpPublic") and p.get("publicPort"):
                host, port = p["ip"], p["publicPort"]
                if ssh_run(host, port, "echo ok", quiet=True):
                    print(f"SSH is up after {int(time.time()-start)}s: root@{host} -p {port}")
                    return host, port
        elapsed = int(time.time() - start)
        if elapsed - last_note >= 30:
            last_note = elapsed
            print(f"  ... still booting, {elapsed}s elapsed (SSH not ready yet)")
        time.sleep(10)
    return None


def download_from_pod(host, port, remote, local) -> bool:
    """Stream a remote file over ssh into a local file. Avoids scp's Windows/
    cygwin path quirks (e.g. it misreading 'C:\\...' as a remote host)."""
    cmd = _ssh_base(host, port) + [f"cat '{remote}'"]
    try:
        with open(local, "wb") as f:
            res = subprocess.run(cmd, stdout=f, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(local) and os.path.getsize(local) > 0
    except Exception:
        return False


def upload_file_to_pod(host, port, local_path, remote_path) -> bool:
    """Stream a local file INTO a remote path over ssh. Uses `cat > remote`
    to sidestep scp on Windows. Creates parent dirs on the pod."""
    parent = os.path.dirname(remote_path)
    if parent:
        ssh_run(host, port, f"mkdir -p '{parent}'", quiet=True, check=False)
    cmd = _ssh_base(host, port) + [f"cat > '{remote_path}'"]
    try:
        with open(local_path, "rb") as f:
            res = subprocess.run(cmd, stdin=f, capture_output=True)
        return res.returncode == 0
    except Exception:
        return False


def sync_local_files_to_pod(host, port, files: Iterable[tuple], repo_root: str) -> int:
    """Upload each (local_relpath, remote_abspath) pair. Returns count uploaded.
    Files that don't exist locally are silently skipped so the caller can pass
    an aspirational list (e.g. optional files added after this refactor)."""
    n = 0
    for rel, remote in files:
        local = os.path.join(repo_root, rel)
        if not os.path.exists(local):
            continue
        if upload_file_to_pod(host, port, local, remote):
            print(f"  synced {rel} -> {remote}")
            n += 1
        else:
            print(f"  [!] failed to sync {rel}")
    return n


def tail_lines_from(host, port, remote_path, start_line):
    """Return (new_complete_lines, next_start_line) from a remote log, 1-indexed
    by line. Only complete (newline-terminated) lines are consumed, so an
    in-progress line (tqdm/pip bar) isn't printed twice."""
    r = ssh_run(host, port, f"tail -n +{start_line} '{remote_path}' 2>/dev/null",
                quiet=True, check=False)
    if not r:
        return [], start_line
    text = r.stdout.decode("utf-8", "replace")
    if not text:
        return [], start_line
    parts = text.split("\n")
    complete = parts[:-1]
    return complete, start_line + len(complete)


# ---------------------------- pod lifecycle ----------------------------
def is_unavailable(err) -> bool:
    m = str(err).lower()
    # RunPod surfaces "no capacity" as several different strings depending on
    # the failure mode (region-wide vs single-host vs GPU-model-wide). Match
    # all the observed variants so we always fall through to the next GPU
    # instead of dying with a hard error.
    return any(s in m for s in (
        "no longer any instances", "no instances", "not available",
        "instances available", "unavailable", "capacity", "out of stock",
        "does not have the resources", "try a different machine",
        "no host available", "no hosts available"))


def create_pod_with_gpu_fallback(runpod, name, dc, volume_id, gpu_candidates,
                                 image=IMAGE, container_disk_gb=CONTAINER_DISK_GB,
                                 mount_path=MOUNT_PATH):
    """Create a pod, trying each GPU in the fallback list until one has capacity.
    Returns the pod dict, or None if none of the candidate GPUs are available.
    Raises RuntimeError on non-availability errors so the caller can die()."""
    for gpu in gpu_candidates:
        print(f"Trying GPU {gpu!r} in {dc} ...")
        try:
            pod = runpod.create_pod(
                name=name, image_name=image, gpu_type_id=gpu,
                cloud_type="SECURE", data_center_id=dc, network_volume_id=volume_id,
                volume_mount_path=mount_path, container_disk_in_gb=container_disk_gb,
                ports="22/tcp", start_ssh=True,
            )
            print(f"  -> got it: {gpu}")
            return pod
        except Exception as e:
            if is_unavailable(e):
                print("  -> unavailable, trying next")
                continue
            raise RuntimeError(f"create_pod failed (not an availability issue): {e}\n"
                               f"Check dc={dc} matches the volume's region and the API key is valid.")
    return None


def boot_pod_with_retries(runpod, name, dc, volume_id, gpu_candidates,
                          boot_timeout=120, boot_retries=5):
    """Create + wait-for-SSH loop with retries. Returns (pod_id, host, port),
    or dies. Terminates any pod that fails to boot so we don't get billed for
    a wedged 'Initializing' host."""
    pod_id = host = port = None
    for attempt in range(1, boot_retries + 1):
        try:
            pod = create_pod_with_gpu_fallback(runpod, name, dc, volume_id, gpu_candidates)
        except RuntimeError as e:
            die(str(e))
        if pod is None:
            die(f"No GPU from the candidate list is available in {dc} right now.\n"
                f"Try again shortly, or pass a different --gpu (see --list).")

        pod_id = pod["id"]
        print(f"Pod created (attempt {attempt}/{boot_retries}): {pod_id}  "
              f"(console: https://www.runpod.io/console/pods)")

        try:
            res = wait_for_ssh(runpod, pod_id, timeout=boot_timeout)
        except KeyboardInterrupt:
            print("\nInterrupted during boot. Terminating pod so it doesn't keep billing...")
            terminate_pod_quiet(runpod, pod_id)
            sys.exit(1)

        if res is not None:
            host, port = res
            return pod_id, host, port

        print(f"Boot timed out after {boot_timeout}s (host likely stuck 'Initializing'). "
              f"Terminating pod {pod_id} and trying a fresh host...")
        terminate_pod_quiet(runpod, pod_id)
        pod_id = None

    die(f"Gave up after {boot_retries} pods failed to boot within {boot_timeout}s each. "
        f"{dc} may be having a rough time -- try again later or a different --dc.")


def terminate_pod_quiet(runpod, pod_id) -> None:
    try:
        runpod.terminate_pod(pod_id)
    except Exception as e:
        print(f"[!] could not terminate pod {pod_id}: {e} -- delete it manually in the console!")
