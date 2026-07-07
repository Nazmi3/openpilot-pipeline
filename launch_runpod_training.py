#!/usr/bin/env python3
"""
launch_runpod_training.py
=========================
One command from your PC -> a RunPod pod is created with your network volume,
then EVERYTHING is (re)provisioned idempotently and training starts, exactly
like the manual run. Self-contained and self-healing:

  * If the volume already has the conda env + dataset -> it skips setup and
    just trains (fast).
  * If the volume is EMPTY (fresh volume, deleted pod, brand-new volume) ->
    it installs miniconda, builds the `optrain` env, clones the pipeline repo,
    downloads comma2k19 (~94GB), sets up wandb, and trains -- from nothing.

So it "just works" whether or not a pod / prior setup exists.

--------------------------------------------------------------------------
ONE-TIME SETUP ON YOUR PC
--------------------------------------------------------------------------
1. pip install runpod requests
2. RunPod API key (https://www.runpod.io/console/user/settings -> API Keys):
       PowerShell:  $env:RUNPOD_API_KEY = "your_key"
3. (only needed if the volume has NO saved wandb login, e.g. a fresh volume)
       $env:WANDB_API_KEY = "your_wandb_key"   # from https://wandb.ai/authorize
4. Your ~/.ssh/id_ed25519 public key must be in your RunPod account SSH keys.

--------------------------------------------------------------------------
USAGE
--------------------------------------------------------------------------
python launch_runpod_training.py --list                       # discover volumes/GPUs
python launch_runpod_training.py --date-it run1 --epochs 15               # create + train
python launch_runpod_training.py --date-it run1 --epochs 15 --auto-stop   # + stop pod when done
python launch_runpod_training.py --date-it run1 --epochs 15 --wait        # + download model & DELETE pod
python launch_runpod_training.py --gen-gt --date-it fullrun --wait        # full GT, train, download, delete
python launch_runpod_training.py --no-train                   # just provision, no training

Billing: the pod bills while running. With --auto-stop it stops itself when
training finishes. The network volume persists everything between runs.
"""

import argparse
import os
import subprocess
import sys
import time

# ---------------------------- CONFIG (env-overridable) ----------------------------
VOLUME_ID   = os.environ.get("RUNPOD_VOLUME_ID", "z4zbwcmpuv")  # 'military_salmon_marsupial' (comma2k19)
DATA_CENTER = os.environ.get("RUNPOD_DC", "EUR-IS-1")           # MUST match the volume's region
GPU_TYPE    = os.environ.get("RUNPOD_GPU", "NVIDIA RTX 4000 Ada Generation")
# Fallback GPUs (all comfortably fit this small model, ~6GB). Tried in order in
# the SAME data center as the volume until one has availability. Cheaper first.
GPU_CANDIDATES = [
    "NVIDIA RTX 4000 Ada Generation",
    "NVIDIA RTX 2000 Ada Generation",
    "NVIDIA RTX A4000",
    "NVIDIA RTX 4000 SFF Ada Generation",
    "NVIDIA RTX A4500",
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
IMAGE       = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
CONTAINER_DISK_GB = 30
MOUNT_PATH  = "/workspace"

WANDB_ENTITY  = os.environ.get("WANDB_ENTITY", "nazmiryuki")
WANDB_PROJECT = os.environ.get("WANDB_PROJECT", "openpilot-pipeline")

SSH_KEY = os.path.expanduser(os.environ.get("RUNPOD_SSH_KEY", "~/.ssh/id_ed25519"))
API_KEY = os.environ.get("RUNPOD_API_KEY")
WANDB_KEY = os.environ.get("WANDB_API_KEY", "")

# --------------------------------------------------------------------------------
# The idempotent provisioning + training script that runs ON THE POD. It reads all
# its parameters from environment variables (set by the launcher), so there is no
# fragile string-formatting against bash's own ${...}. Every step is skip-if-present,
# so running it on an already-provisioned volume is fast (it just trains).
# --------------------------------------------------------------------------------
PROVISION_SCRIPT = r'''#!/usr/bin/env bash
set -uo pipefail
WS=/workspace
CONDA=$WS/miniconda3
ENVN=optrain
REPO=$WS/openpilot-pipeline
DATA=$WS/comma2k19
PIPE_REPO=https://github.com/nikebless/openpilot-pipeline.git
PIN=b613373
TORCH_INDEX=https://download.pytorch.org/whl/cu113

: "${DATE_IT:=run1}" "${EPOCHS:=15}" "${BATCH:=8}" "${GEN_GT:=0}" "${AUTO_STOP:=0}"
: "${LOG_FREQ:=10}" "${VAL_FREQ:=200}"
: "${WB_ENTITY:=nazmiryuki}" "${WB_PROJECT:=openpilot-pipeline}"
: "${WANDB_API_KEY:=}" "${RUNPOD_API_KEY:=}"

echo ">>> provision start $(date)"

# clear any stale train log from a previous run with the same DATE_IT, so the
# launcher's completion poll can't detect an OLD 'training_finished' marker
# (that caused repeat runs to "finish" instantly).
rm -f "$WS/train_${DATE_IT}.log" 2>/dev/null || true

# 1) system deps
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 || true
apt-get install -y -qq git curl unzip file build-essential libgl1 libglu1-mesa \
  libglib2.0-0 libx11-6 libxext6 libxrandr2 libxinerama1 libxcursor1 libxi6 >/dev/null 2>&1 || true

# 2) miniconda (on the volume)
if [ ! -x "$CONDA/bin/conda" ]; then
  echo ">>> installing miniconda"
  curl -fsSL -o /tmp/mc.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
  bash /tmp/mc.sh -b -p "$CONDA"; rm -f /tmp/mc.sh
fi
source "$CONDA/etc/profile.d/conda.sh"
conda config --set auto_activate_base false >/dev/null 2>&1 || true
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/main >/dev/null 2>&1 || true
conda tos accept --override-channels --channel https://repo.anaconda.com/pkgs/r    >/dev/null 2>&1 || true

# 3) pipeline repo
if [ ! -d "$REPO/.git" ]; then echo ">>> cloning pipeline"; git clone "$PIPE_REPO" "$REPO"; fi
cd "$REPO"; git checkout "$PIN" 2>/dev/null || true

# 4) conda env (conda packages only; strip pip section)
if ! conda env list | awk '{print $1}' | grep -qx "$ENVN"; then
  echo ">>> creating conda env (slow first time)"
  sed '/^[[:space:]]*-[[:space:]]*pip:[[:space:]]*$/,$d' environment.yml > /tmp/env.conda.yml
  conda env create -f /tmp/env.conda.yml -n "$ENVN"
fi
conda activate "$ENVN"

# 5) modernized pip stage (only if torch not yet installed)
if ! python -c "import torch" 2>/dev/null; then
  echo ">>> installing pip deps (modernized)"
  grep -E '^[[:space:]]{4}-[[:space:]]' environment.yml | sed -E 's/^[[:space:]]*-[[:space:]]*//' \
    | grep -vE '^(attr==0\.3\.1|sklearn==0\.0|onnxruntime-gpu==)' > /tmp/reqs.txt
  grep -q '^h5py==' /tmp/reqs.txt || echo 'h5py==2.10.0' >> /tmp/reqs.txt
  pip install "setuptools==65.5.1" "wheel==0.38.4"
  pip install --no-build-isolation -r /tmp/reqs.txt --extra-index-url "$TORCH_INDEX"
fi

# 6) patch generate_gt.py (env-configurable threads + skip unused calib)
GT="$REPO/gt_distill/generate_gt.py"
grep -q 'GT_THREADS' "$GT" || sed -i 's|options.intra_op_num_threads = 30|options.intra_op_num_threads = int(os.environ.get("GT_THREADS","30"))|' "$GT"
grep -q 'skip calib' "$GT" || sed -i 's|^\( *\)save_segment_calib(dir_path|\1pass # skip calib: save_segment_calib(dir_path|' "$GT"

# 7) dataset (download comma2k19 if not present)
if [ -z "$(find "$DATA" -name video.hevc 2>/dev/null | head -1)" ]; then
  echo ">>> downloading comma2k19 (~94GB)"
  "$CONDA/bin/pip" install -q huggingface_hub hf_transfer
  export HF_HUB_ENABLE_HF_TRANSFER=1
  mkdir -p "$DATA" "$WS/comma2k19_zips"
  for i in $(seq 1 10); do
    f="raw_data/Chunk_${i}.zip"
    "$CONDA/bin/hf" download commaai/comma2k19 --repo-type dataset --include "$f" --local-dir "$WS/comma2k19_zips"
    unzip -n -q "$WS/comma2k19_zips/$f" -d "$DATA" && rm -f "$WS/comma2k19_zips/$f"
  done
fi

# 8) wandb credentials: prefer the one saved on the volume, else the env key
if [ -f "$WS/.netrc" ]; then
  cp "$WS/.netrc" /root/.netrc; chmod 600 /root/.netrc
elif [ -n "$WANDB_API_KEY" ]; then
  printf 'machine api.wandb.ai\n  login user\n  password %s\n' "$WANDB_API_KEY" > /root/.netrc
  chmod 600 /root/.netrc; cp /root/.netrc "$WS/.netrc"
else
  echo ">>> [warn] no wandb credentials (volume .netrc missing and WANDB_API_KEY unset)"
fi

# 9) optional: full ground-truth generation
if [ "$GEN_GT" = "1" ]; then
  echo ">>> generating full ground truth"
  cd "$REPO"
  find "$DATA" -name video.hevc -printf '%h\n' | sort -u > "$WS/gt_segments.txt"
  N=16; T=8; SD="$WS/gt_shards"; mkdir -p "$SD" "$WS/gt_logs"; rm -f "$SD"/s_*.txt
  awk -v n=$N -v d="$SD" '{print > (d "/s_" (NR%n) ".txt")}' "$WS/gt_segments.txt"
  for i in $(seq 0 $((N-1))); do
    [ -s "$SD/s_$i.txt" ] || continue
    GT_THREADS=$T OMP_NUM_THREADS=$T python gt_distill/generate_gt.py \
      --recordings_basedir "$DATA" --cache "$SD/s_$i.txt" \
      --openpilot_dir "$REPO/common" > "$WS/gt_logs/w_$i.log" 2>&1 &
  done
  wait
  echo ">>> GT done: $(find "$DATA" -name gt_distill.h5 | wc -l) files"
fi

# 10) train. train.py sometimes hangs on exit (dataloader/wandb threads don't
#     close), which would block auto-stop / the launcher forever. So run it in
#     the background and force-exit once it logs "training_finished" (the model
#     is already saved by that point).
echo ">>> starting training ($DATE_IT) $(date)"
cd "$REPO/train"
TLOG="$WS/train_${DATE_IT}.log"
PYTHONPATH="$REPO" WANDB_ENTITY="$WB_ENTITY" WANDB_PROJECT="$WB_PROJECT" \
  python train.py --date_it "$DATE_IT" --recordings_basedir "$DATA" \
  --batch_size "$BATCH" --epochs "$EPOCHS" \
  --log_frequency "$LOG_FREQ" --val_frequency "$VAL_FREQ" > "$TLOG" 2>&1 &
TRAIN_PID=$!
while kill -0 "$TRAIN_PID" 2>/dev/null; do
  if grep -q "training_finished" "$TLOG" 2>/dev/null; then
    sleep 10; kill "$TRAIN_PID" 2>/dev/null; pkill -P "$TRAIN_PID" 2>/dev/null; break
  fi
  sleep 15
done
wait "$TRAIN_PID" 2>/dev/null
echo ">>> TRAINING FINISHED $(date)"

# 11) optional: stop the pod to end GPU billing
if [ "$AUTO_STOP" = "1" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
  echo ">>> auto-stopping pod $RUNPOD_POD_ID"
  curl -s -X POST "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID/stop" \
    -H "Authorization: Bearer $RUNPOD_API_KEY" || runpodctl stop pod "$RUNPOD_POD_ID" || true
fi
'''


# --------------------------------------------------------------------------------
def die(msg):
    print("ERROR:", msg, file=sys.stderr)
    sys.exit(1)


def get_runpod():
    try:
        import runpod
    except ImportError:
        die("pip install runpod   (and: pip install requests)")
    if not API_KEY:
        die("set RUNPOD_API_KEY env var (RunPod Console -> Settings -> API Keys)")
    runpod.api_key = API_KEY
    return runpod


def list_resources():
    runpod = get_runpod()
    print("\n=== GPU types ===")
    try:
        for g in runpod.get_gpus():
            print(f"  {g.get('id')}")
    except Exception as e:
        print("  (could not list GPUs:", e, ")")
    print("\n=== Your network volumes (use the id as --volume) ===")
    try:
        import requests
        r = requests.get("https://rest.runpod.io/v1/networkvolumes",
                         headers={"Authorization": f"Bearer {API_KEY}"}, timeout=30)
        r.raise_for_status()
        for v in r.json() or []:
            print(f"  id={v.get('id')}  name={v.get('name')!r}  "
                  f"dc={v.get('dataCenterId')}  size={v.get('size')}GB")
    except Exception as e:
        print("  (REST list failed:", e, ") -> RunPod Console -> Storage -> volume -> ID")
    print()


def ssh_run(host, port, command, stdin=None, quiet=False, check=True):
    cmd = ["ssh",
           "-o", "StrictHostKeyChecking=no",       # ephemeral pods; skip host-key prompts
           "-o", "UserKnownHostsFile=/dev/null",    # don't try to write ~/.ssh/known_hosts
           "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
           "-i", SSH_KEY, "-p", str(port), f"root@{host}", command]
    # Always send stdin as LF-normalized bytes so scripts run under Linux bash
    # regardless of this file's (Windows) line endings.
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


def wait_for_ssh(runpod, pod_id, timeout=600):
    print("Waiting for the pod to boot and expose SSH...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pod = runpod.get_pod(pod_id)
        except Exception:
            pod = None
        for p in ((pod or {}).get("runtime") or {}).get("ports") or []:
            if p.get("privatePort") == 22 and p.get("isIpPublic") and p.get("publicPort"):
                host, port = p["ip"], p["publicPort"]
                if ssh_run(host, port, "echo ok", quiet=True):
                    print(f"SSH is up: root@{host} -p {port}")
                    return host, port
        time.sleep(10)
    die("timed out waiting for SSH. Check the pod in the RunPod console.")


def start_remote(host, port, args):
    # remove stale logs from a prior run with the same date_it BEFORE launching,
    # so the --wait poll can't see an old 'training_finished'/'TRAINING FINISHED'.
    ssh_run(host, port,
            f"rm -f /workspace/train_{args.date_it}.log /workspace/launch_{args.date_it}.log",
            quiet=True, check=False)
    # upload the self-contained provisioning script...
    print("Uploading provisioning script to the pod...")
    if ssh_run(host, port, "cat > /workspace/_provision.sh && chmod +x /workspace/_provision.sh",
               stdin=PROVISION_SCRIPT) is None:
        die("failed to upload provisioning script over SSH")
    # ...and run it fully detached with all parameters passed via env.
    envs = {
        "DATE_IT": args.date_it, "EPOCHS": str(args.epochs), "BATCH": str(args.batch_size),
        "GEN_GT": "1" if args.gen_gt else "0",
        # in --wait mode the launcher downloads the model then terminates the pod,
        # so the pod must NOT stop itself first.
        "AUTO_STOP": "1" if (args.auto_stop and not args.wait) else "0",
        "LOG_FREQ": str(args.log_frequency), "VAL_FREQ": str(args.val_frequency),
        "WB_ENTITY": WANDB_ENTITY, "WB_PROJECT": WANDB_PROJECT,
        "WANDB_API_KEY": WANDB_KEY, "RUNPOD_API_KEY": API_KEY,
    }
    env_prefix = " ".join(f'{k}="{v}"' for k, v in envs.items())
    launch = (f"setsid nohup env {env_prefix} bash /workspace/_provision.sh "
              f"> /workspace/launch_{args.date_it}.log 2>&1 < /dev/null & echo LAUNCHED pid=$!")
    print("Launching provision+train on the pod...")
    ssh_run(host, port, f"bash -lc '{launch}'")


def download_from_pod(host, port, remote, local):
    """Stream a remote file over ssh into a local file. Avoids scp's Windows/
    cygwin path quirks (e.g. it misreading 'C:\\...' as a remote host)."""
    cmd = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
           "-o", "LogLevel=ERROR", "-o", "BatchMode=yes", "-o", "ConnectTimeout=25",
           "-i", SSH_KEY, "-p", str(port), f"root@{host}", f"cat '{remote}'"]
    try:
        with open(local, "wb") as f:
            res = subprocess.run(cmd, stdout=f, stderr=subprocess.DEVNULL)
        return res.returncode == 0 and os.path.exists(local) and os.path.getsize(local) > 0
    except Exception:
        return False


def wait_download_terminate(host, port, pod_id, runpod, args):
    """Block until training finishes, download the model to ./trained_models/,
    then terminate the pod. The model dir is on the volume, so even if download
    fails the model is not lost (a later pod can still fetch it)."""
    log = f"/workspace/launch_{args.date_it}.log"
    tlog = f"/workspace/train_{args.date_it}.log"
    train_dir = "/workspace/openpilot-pipeline/train/nets"
    remote_model = f"{train_dir}/model_itr/{args.date_it}.pth"

    print("\n--wait: staying up until training finishes (polling every 60s).")
    print("Keep this window open. Ctrl+C stops watching but NOT the pod.")
    while True:
        r = ssh_run(host, port,
                    f"if grep -q 'TRAINING FINISHED' {log} 2>/dev/null || "
                    f"grep -q 'training_finished' {tlog} 2>/dev/null || "
                    f"! pgrep -f '[_]provision.sh' >/dev/null 2>&1; then echo DONE; else echo RUNNING; fi",
                    quiet=True, check=False)
        state = r.stdout.decode("utf-8", "replace") if r else ""
        if "DONE" in state:
            break
        time.sleep(60)
    print("Training finished (or the run ended).")

    # Locate the model: prefer the final model, else the newest checkpoint.
    find = ssh_run(host, port,
                   f"if [ -f {remote_model} ]; then echo FINAL {remote_model}; "
                   f"else ls -t {train_dir}/checkpoints/*.pth 2>/dev/null | head -1 | sed 's/^/CKPT /'; fi",
                   quiet=True, check=False)
    line = (find.stdout.decode("utf-8", "replace").strip() if find else "")
    local_dir = os.path.join(os.getcwd(), "trained_models")
    os.makedirs(local_dir, exist_ok=True)

    if line.startswith("FINAL ") or line.startswith("CKPT "):
        kind, remote_path = line.split(" ", 1)
        local_path = os.path.join(local_dir, f"{args.date_it}.pth" if kind == "FINAL"
                                   else os.path.basename(remote_path.strip()))
        print(f"Downloading {kind} model:\n  {remote_path.strip()}\n  -> {local_path}")
        if download_from_pod(host, port, remote_path.strip(), local_path):
            print(f"  saved: {local_path} ({os.path.getsize(local_path)//(1024*1024)} MB)")
        else:
            print("  [!] download failed. The model is still on the volume at:")
            print(f"      {remote_path.strip()}  (fetch it later with another pod)")
    else:
        print("[!] No model file found — training may have crashed. "
              "Check the wandb run / pod logs. Nothing to download.")

    print(f"Terminating pod {pod_id} ...")
    try:
        runpod.terminate_pod(pod_id)
        print("Pod terminated. (Volume + everything on it is preserved.)")
    except Exception as e:
        print(f"[!] terminate failed: {e} — delete it manually in the console.")


def main():
    ap = argparse.ArgumentParser(description="Create a RunPod pod and (re)provision + train.")
    ap.add_argument("--list", action="store_true", help="list GPUs + network volumes, then exit")
    ap.add_argument("--volume", default=VOLUME_ID, help="network volume ID (or set RUNPOD_VOLUME_ID)")
    ap.add_argument("--gpu", default=GPU_TYPE, help="preferred GPU type id (tried first, then fallbacks)")
    ap.add_argument("--strict-gpu", action="store_true", help="only use --gpu, no automatic fallback")
    ap.add_argument("--dc", default=DATA_CENTER, help="data center id (must match the volume)")
    ap.add_argument("--date-it", default="run1", help="wandb run name")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch-size", type=int, default=8)
    ap.add_argument("--log-frequency", type=int, default=10, help="log train loss to wandb every N steps")
    ap.add_argument("--val-frequency", type=int, default=200, help="run validation every N steps")
    ap.add_argument("--gen-gt", action="store_true", help="generate FULL ground truth before training")
    ap.add_argument("--no-train", action="store_true", help="just provision; don't start training")
    ap.add_argument("--auto-stop", action="store_true", help="stop the pod when training finishes")
    ap.add_argument("--wait", action="store_true",
                    help="keep running until training finishes, download the model to "
                         "./trained_models/, then TERMINATE the pod")
    args = ap.parse_args()

    runpod = get_runpod()
    if args.list:
        list_resources()
        return
    if not args.volume:
        die("no network volume set. Run --list to find its ID, then pass --volume <ID>.")

    # Build the GPU try-list: the requested one first, then the fallbacks
    # (deduped, order preserved). All must be in the volume's data center.
    try_gpus = [args.gpu] + [g for g in GPU_CANDIDATES if g != args.gpu]
    if args.strict_gpu:
        try_gpus = [args.gpu]

    def is_unavailable(err):
        m = str(err).lower()
        return any(s in m for s in (
            "no longer any instances", "no instances", "not available",
            "instances available", "unavailable", "capacity", "out of stock"))

    pod = None
    for gpu in try_gpus:
        print(f"Trying GPU {gpu!r} in {args.dc} ...")
        try:
            pod = runpod.create_pod(
                name=f"optrain-{args.date_it}", image_name=IMAGE, gpu_type_id=gpu,
                cloud_type="SECURE", data_center_id=args.dc, network_volume_id=args.volume,
                volume_mount_path=MOUNT_PATH, container_disk_in_gb=CONTAINER_DISK_GB,
                ports="22/tcp", start_ssh=True,
            )
            print(f"  -> got it: {gpu}")
            break
        except Exception as e:
            if is_unavailable(e):
                print(f"  -> unavailable, trying next")
                continue
            die(f"create_pod failed (not an availability issue): {e}\n"
                f"Check --dc matches the volume's region and the API key is valid.")
    if pod is None:
        die(f"No GPU from the candidate list is available in {args.dc} right now.\n"
            f"Try again shortly, or pass a different --gpu (see --list). "
            f"Use --strict-gpu to only try the one you request.")

    pod_id = pod["id"]
    print(f"Pod created: {pod_id}  (console: https://www.runpod.io/console/pods)")

    host, port = wait_for_ssh(runpod, pod_id)

    if args.no_train:
        print("Provisioning without training...")
        # still upload+run provision but skip training by pointing to a no-op? Simpler:
        # run provision up to setup by asking user to SSH. Here we just upload it.
        ssh_run(host, port, "cat > /workspace/_provision.sh && chmod +x /workspace/_provision.sh",
                stdin=PROVISION_SCRIPT)
        print(f"Uploaded /workspace/_provision.sh. SSH in and run it, or omit --no-train next time.")
        print(f"SSH: ssh -i {SSH_KEY} -p {port} root@{host}")
        return

    start_remote(host, port, args)

    print("\n=========================================================")
    print(f" Pod:    {pod_id}")
    print(f" SSH:    ssh -i {SSH_KEY} -p {port} root@{host}")
    print(f" Live:   tail -f /workspace/launch_{args.date_it}.log        (provision+train)")
    print(f"         tail -f /workspace/train_{args.date_it}.log   is inside that too")
    print(f" wandb:  https://wandb.ai/{WANDB_ENTITY}/{WANDB_PROJECT}")
    if args.wait:
        print(" --wait: ON - will download the model to ./trained_models/ and")
        print("         TERMINATE the pod when training finishes (keep this open).")
    elif args.auto_stop:
        print(" Auto-stop: ON - pod stops itself when training finishes.")
    else:
        print(" Remember to stop/delete the pod when done (billing!).")
    print(" First run on an EMPTY volume provisions everything (~1h); after that it's fast.")
    print("=========================================================")

    if args.wait:
        wait_download_terminate(host, port, pod_id, runpod, args)


if __name__ == "__main__":
    main()
