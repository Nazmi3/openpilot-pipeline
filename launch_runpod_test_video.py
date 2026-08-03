#!/usr/bin/env python3
"""
launch_runpod_test_video.py
===========================
One command from your PC -> a RunPod pod is created, the local test script
`train/test_prediction_video.py` is uploaded, it runs against the model + a
random dataset segment on the network volume, and the resulting 1-minute
prediction-video mp4 is downloaded locally. Then the pod is terminated.

This is the *test-time* sibling of `launch_runpod_training.py`. It shares
all pod plumbing (SSH, GPU fallback, boot retries, downloads) with the
training launcher through `runpod_lib`, so behavior stays consistent.

USAGE (from your PC)
--------------------
    python launch_runpod_test_video.py                        # defaults (run1)
    python launch_runpod_test_video.py --model run1 --duration 60
    python launch_runpod_test_video.py --segment /workspace/comma2k19/Chunk_1/...
    python launch_runpod_test_video.py --model run1 --list-segments

Prereqs (same as the training launcher):
    * RUNPOD_API_KEY set once (setx RUNPOD_API_KEY "...")
    * ~/.ssh/id_ed25519 uploaded to RunPod SSH keys
    * The network volume already provisioned by a prior training run
      (has conda env `optrain`, cloned repo, and `comma2k19/`).
      If it is empty, this launcher errors out with an instructive message
      instead of doing a slow one-off bootstrap -- do that with
      `launch_runpod_training.py` instead.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from runpod_lib import (
    VOLUME_ID, DATA_CENTER, GPU_TYPE, GPU_CANDIDATES, MOUNT_PATH,
    SSH_KEY, API_KEY,
    die, get_runpod, list_resources, ssh_run, download_from_pod,
    sync_local_files_to_pod, tail_lines_from,
    boot_pod_with_retries, terminate_pod_quiet,
)


# Test-time inference runs on **CPU** on the pod (see POD_SCRIPT).
#
# Rationale: `run1.pth` was trained with the pinned torch (cu113) whose CUDA
# capabilities top out at sm_86 (Ampere). Ada Lovelace (sm_89: RTX 4000-6000
# Ada, L4/L40, 4090, RTX PRO 5000/6000 Ada) and Blackwell (sm_120) LOAD the
# weights but fail at the first conv with `CUDNN_STATUS_NOT_INITIALIZED`, and
# Ampere hosts are frequently unavailable in the volume's DC (EUR-IS-1). CPU
# side-steps the whole mess: any GPU host works because the GPU isn't used.
# Supercombo on CPU over ~1200 frames finishes in a few minutes -- acceptable
# for a one-off test.
#
# We still ask for a GPU host (RunPod pricing is per-host, not per-GPU-usage);
# the GPU just sits idle. Use the broad list to maximize scheduler success.
GPU_CANDIDATES_TEST = GPU_CANDIDATES


REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
POD_REPO = "/workspace/openpilot-pipeline"
POD_DATA = "/workspace/comma2k19"
POD_MODEL_DIR = f"{POD_REPO}/train/nets/model_itr"
POD_LOG = "/workspace/test_video.log"


# --------------------------------------------------------------------------------
# The bash script that runs ON the pod. Reads everything through env vars set by
# the launcher. Assumes the volume is provisioned; fails loudly if not.
# --------------------------------------------------------------------------------
POD_SCRIPT = r'''#!/usr/bin/env bash
set -uo pipefail
WS=/workspace
CONDA=$WS/miniconda3
ENVN=optrain
REPO=$WS/openpilot-pipeline
DATA=$WS/comma2k19
# Container-local scratch (fast SSD; NOT the moosefs network volume).
# We stage the input video + model here to sidestep pathological read
# latency when the volume is under load.
STAGE=/root/pred_stage
mkdir -p "$STAGE"

: "${DATE_IT:=run1}" "${DURATION:=60}" "${FPS:=20}"
: "${SEGMENT:=}" "${OUTPUT:=}"
: "${NUM_SEGMENTS:=1}" "${SEED:=42}"

echo ">>> test-video start $(date)"

# Sanity: volume must already be provisioned (by a prior training run).
if [ ! -x "$CONDA/bin/conda" ] || [ ! -d "$REPO/.git" ]; then
  echo ">>> ERROR: volume is not provisioned. Run launch_runpod_training.py first."
  exit 2
fi
source "$CONDA/etc/profile.d/conda.sh"
conda activate "$ENVN" || { echo ">>> ERROR: conda env '$ENVN' missing"; exit 2; }

# Locate a model to test. Prefer the exact DATE_IT.pth; fall back to any .pth in
# the model_itr / checkpoints tree.
MODEL_PTH="$REPO/train/nets/model_itr/${DATE_IT}.pth"
if [ ! -f "$MODEL_PTH" ]; then
  ALT=$(find "$REPO/train/nets" -name '*.pth' 2>/dev/null | head -1)
  if [ -n "$ALT" ]; then
    echo ">>> ${DATE_IT}.pth not found, falling back to: $ALT"
    MODEL_PTH="$ALT"
  else
    echo ">>> ERROR: no .pth found under $REPO/train/nets"
    exit 3
  fi
fi

# --- choose segments ---------------------------------------------------------
# Prefer segments that have GT (same population the model trained on). When
# NUM_SEGMENTS > 1 we pick at random, seeded so a run is reproducible.
# An explicit $SEGMENT always wins and forces a single segment.
SEG_LIST="$WS/pred_segments.txt"
: > "$SEG_LIST"
if [ -n "$SEGMENT" ]; then
  echo "$SEGMENT" > "$SEG_LIST"
else
  find "$DATA" -name gt_distill.h5 -printf '%h\n' 2>/dev/null | sort > "$WS/pred_pool.txt"
  if [ ! -s "$WS/pred_pool.txt" ]; then
    find "$DATA" -name video.hevc -printf '%h\n' 2>/dev/null | sort > "$WS/pred_pool.txt"
  fi
  POOL=$(wc -l < "$WS/pred_pool.txt")
  if [ "$POOL" -eq 0 ]; then
    echo ">>> ERROR: could not find any driving segment under $DATA"; exit 4
  fi
  echo ">>> segment pool: $POOL with ground truth; picking $NUM_SEGMENTS (seed=$SEED)"
  # deterministic shuffle: --random-source makes shuf reproducible for a seed
  shuf --random-source=<(yes "$SEED") -n "$NUM_SEGMENTS" "$WS/pred_pool.txt" > "$SEG_LIST"
fi
echo ">>> model:    $MODEL_PTH"

# --- stage inputs onto local disk (avoids repeated moosefs reads during
# inference). Copies are large but one-shot; local reads afterward are fast. ---
# Each segment gets its own staged dir containing the video AND global_pose/
# (only ~130KB) so per-segment calibration resolves locally, with no further
# moosefs access once inference starts.
SEG_ARGS=""
i=0
while IFS= read -r SEG; do
  [ -n "$SEG" ] || continue
  if [ ! -d "$SEG" ]; then echo ">>> [warn] not a directory, skipping: $SEG"; continue; fi
  VIDEO_SRC=""
  for name in video.hevc fcamera.hevc; do
    if [ -f "$SEG/$name" ]; then VIDEO_SRC="$SEG/$name"; break; fi
  done
  if [ -z "$VIDEO_SRC" ]; then
    echo ">>> [warn] no video in $SEG, skipping"; continue
  fi
  D="$STAGE/seg$i"
  mkdir -p "$D"
  echo ">>> staging seg$i ($(du -h "$VIDEO_SRC" | cut -f1)): $SEG"
  timeout 300 cp "$VIDEO_SRC" "$D/$(basename "$VIDEO_SRC")" || {
    echo ">>> [warn] staging timed out for $SEG, skipping"; continue;
  }
  # calibration source (tiny) -- lets the renderer compute this segment's own rpy
  if [ -d "$SEG/global_pose" ]; then
    cp -r "$SEG/global_pose" "$D/" 2>/dev/null || echo ">>> [warn] global_pose copy failed for $SEG"
  else
    echo ">>> [warn] no global_pose in $SEG -- calibration will fall back to zero"
  fi
  SEG_ARGS="$SEG_ARGS $D"
  i=$((i+1))
done < "$SEG_LIST"

if [ "$i" -eq 0 ]; then
  echo ">>> ERROR: no segments could be staged"; exit 4
fi
echo ">>> staged $i segment(s)"

LOCAL_MODEL="$STAGE/$(basename "$MODEL_PTH")"
echo ">>> staging model ($(du -h "$MODEL_PTH" | cut -f1))..."
time timeout 300 cp "$MODEL_PTH" "$LOCAL_MODEL" || {
  echo ">>> ERROR: staging model timed out"; exit 5;
}
# Also stage supercombo.onnx (the trainable-model loader needs it at import).
SUPER_SRC="$REPO/common/models/supercombo.onnx"
if [ -f "$SUPER_SRC" ]; then
  time timeout 300 cp "$SUPER_SRC" "$STAGE/supercombo.onnx" || true
fi

# Pre-warm the python module imports so any cold moosefs miss happens now,
# in a labeled step, before we start the real script. `python -u` forces
# unbuffered stdout so each print line reaches the log immediately.
echo ">>> pre-warming python imports (first cold import can be slow on moosefs)"
time python -u -c "import time; t=time.time(); import numpy, cv2, onnx, torch, onnx2pytorch, onnxruntime, h5py; print('imports ok in %.1fs' % (time.time()-t))" \
  || { echo ">>> ERROR: import warmup failed"; exit 6; }

# Output goes to a stable path the launcher then downloads.
# Filename tags the mode so single-model vs comparison vs montage runs don't
# overwrite each other in ./trained_models/.
if [ -z "$OUTPUT" ]; then
  if [ -n "${MONTAGE_FRAME:-}" ]; then
    OUTPUT="$WS/prediction_${DATE_IT}_calib_montage.png"
  elif [ "${COMPARE_TEACHER:-1}" = "1" ]; then
    OUTPUT="$WS/prediction_${DATE_IT}_${DURATION}s_teacher_vs_student.mp4"
  else
    OUTPUT="$WS/prediction_${DATE_IT}_${DURATION}s.mp4"
  fi
fi
echo ">>> output:   $OUTPUT"

# Optional teacher model for the side-by-side comparison view. We already
# staged supercombo.onnx above; only pass it if it made it there.
# (The calibration montage is student-only, so skip the teacher there.)
TEACHER_ARG=""
if [ -z "${MONTAGE_FRAME:-}" ] && [ "${COMPARE_TEACHER:-1}" = "1" ] && [ -f "$STAGE/supercombo.onnx" ]; then
  TEACHER_ARG="--teacher-model $STAGE/supercombo.onnx"
  echo ">>> teacher:   $STAGE/supercombo.onnx (side-by-side)"
fi

# Debug: calibration montage instead of a video.
MONTAGE_ARG=""
if [ -n "${MONTAGE_FRAME:-}" ]; then
  MONTAGE_ARG="--calib-montage $MONTAGE_FRAME"
  echo ">>> calib montage at frame $MONTAGE_FRAME"
fi

# Manual calibration override (radians, "roll pitch yaw"). When set, skips the
# auto global_pose estimate so you can hand-tune the projection.
CALIB_ARG=""
if [ -n "${RPY_CALIB_RAD:-}" ]; then
  CALIB_ARG="--rpy-calib $RPY_CALIB_RAD"
  echo ">>> manual rpy-calib (rad): $RPY_CALIB_RAD"
fi

# Run the test unbuffered against the local staged files.
#   * CUDA_VISIBLE_DEVICES=""  -> force CPU inference (see note in launcher).
#   * OMP/MKL threads: unbounded numpy would spawn ~all-cores, some hosts
#     over-subscribe and stall; cap to a sensible number for ~130 GFLOPs/frame.
# Note: no --segment-logs needed -- each staged segment carries its own
# global_pose/, so calibration is resolved per segment from local disk.
cd "$REPO/train"
CUDA_VISIBLE_DEVICES="" \
  OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}" MKL_NUM_THREADS="${MKL_NUM_THREADS:-8}" \
  PYTHONPATH="$REPO" python -u test_prediction_video.py \
  --model "$LOCAL_MODEL" --segments $SEG_ARGS --output "$OUTPUT" \
  --duration "$DURATION" --fps "$FPS" $TEACHER_ARG $MONTAGE_ARG $CALIB_ARG \
  --openpilot-dir "$REPO/common"
RC=$?

if [ $RC -ne 0 ] || [ ! -f "$OUTPUT" ]; then
  echo ">>> ERROR: test_prediction_video.py failed (rc=$RC)"
  exit $RC
fi

echo ">>> TEST VIDEO FINISHED $(date)"
echo ">>> OUTPUT_PATH=$OUTPUT"
'''


# --------------------------------------------------------------------------------
# Local files that the pod needs (repo is pinned to an old SHA; we overlay our
# working-tree copies so pod-side code matches what we've iterated on locally).
# --------------------------------------------------------------------------------
FILES_TO_SYNC = [
    # (local repo-relative path, absolute path on the pod)
    ("utils.py",                          f"{POD_REPO}/utils.py"),
    ("train/dataloader.py",               f"{POD_REPO}/train/dataloader.py"),
    ("train/model.py",                    f"{POD_REPO}/train/model.py"),
    ("train/train.py",                    f"{POD_REPO}/train/train.py"),
    ("train/test_prediction_video.py",    f"{POD_REPO}/train/test_prediction_video.py"),
]


def _rpy_deg_to_rad_str(rpy_deg):
    """Parse a 'roll pitch yaw' string in DEGREES and return a space-separated
    string in RADIANS for the pod's --rpy-calib. Empty string when unset."""
    if not rpy_deg:
        return ""
    import math
    parts = [p for p in rpy_deg.replace(",", " ").split() if p]
    if len(parts) != 3:
        die(f"--rpy-calib expects 3 numbers 'roll pitch yaw' (degrees), got: {rpy_deg!r}")
    try:
        rad = [math.radians(float(p)) for p in parts]
    except ValueError:
        die(f"--rpy-calib values must be numbers, got: {rpy_deg!r}")
    return " ".join(f"{v:.6f}" for v in rad)


def start_remote(host, port, args):
    """Upload the pod script and launch it detached, so the local process can
    stream logs from `POD_LOG` and cleanly Ctrl+C without killing the run."""
    print("\nSyncing locally modified files to the pod's repo copy...")
    n = sync_local_files_to_pod(host, port, FILES_TO_SYNC, REPO_ROOT)
    if n == 0:
        die("no local files were uploaded -- check the paths in FILES_TO_SYNC.")

    print("Uploading pod script...")
    if ssh_run(host, port,
               "cat > /workspace/_test_video.sh && chmod +x /workspace/_test_video.sh",
               stdin=POD_SCRIPT) is None:
        die("failed to upload pod script over SSH")

    envs = {
        "DATE_IT":         args.model,
        "DURATION":        str(args.duration),
        "FPS":             str(args.fps),
        "SEGMENT":         args.segment or "",
        "OUTPUT":          "",  # let the script pick a stable default
        # POD_SCRIPT reads COMPARE_TEACHER=1/0 to decide whether to pass
        # --teacher-model to test_prediction_video.py.
        "COMPARE_TEACHER": "0" if args.no_compare else "1",
        # DEBUG: when set, render a calibration montage PNG at this frame
        # instead of a video (student-only, fast).
        "MONTAGE_FRAME": str(args.calib_montage) if args.calib_montage is not None else "",
        # Manual calibration override: launcher takes DEGREES for ergonomics,
        # the test script wants RADIANS, so convert here.
        "RPY_CALIB_RAD": _rpy_deg_to_rad_str(args.rpy_calib),
        # Multi-segment: how many random GT segments to concatenate, and the
        # seed that makes the pick reproducible.
        "NUM_SEGMENTS": str(args.num_segments),
        "SEED": str(args.seed),
    }
    env_prefix = " ".join(f'{k}="{v}"' for k, v in envs.items())
    launch = (f"setsid nohup env {env_prefix} bash /workspace/_test_video.sh "
              f"> {POD_LOG} 2>&1 < /dev/null & echo LAUNCHED pid=$!")
    print("Launching test-video job on the pod...")
    ssh_run(host, port, f"bash -lc '{launch}'")


def wait_download_terminate(host, port, pod_id, runpod, args):
    """Stream progress; on SUCCESS, download the mp4 and terminate the pod.
    On failure, keep the pod alive so the user can SSH in and inspect."""
    print("\nStreaming pod log until the video is produced. Ctrl+C stops watching (not the pod).\n")
    next_line = 1
    output_path_remote = None
    success_marker_seen = False

    while True:
        new_lines, next_line = tail_lines_from(host, port, POD_LOG, next_line)
        for ln in new_lines:
            s = ln.rstrip("\r")
            if s.strip():
                print("  " + s)
            if s.startswith(">>> OUTPUT_PATH="):
                output_path_remote = s.split("=", 1)[1].strip()
            if "TEST VIDEO FINISHED" in s:
                success_marker_seen = True

        # Done? -- explicit success marker OR the script's process has exited.
        r = ssh_run(host, port,
                    f"if grep -q 'TEST VIDEO FINISHED' {POD_LOG} 2>/dev/null || "
                    f"! pgrep -f '[_]test_video.sh' >/dev/null 2>&1; then echo DONE; else echo RUNNING; fi",
                    quiet=True, check=False)
        state = r.stdout.decode("utf-8", "replace") if r else ""
        if "DONE" in state:
            new_lines, next_line = tail_lines_from(host, port, POD_LOG, next_line)
            for ln in new_lines:
                s = ln.rstrip("\r")
                if s.strip():
                    print("  " + s)
                if s.startswith(">>> OUTPUT_PATH="):
                    output_path_remote = s.split("=", 1)[1].strip()
                if "TEST VIDEO FINISHED" in s:
                    success_marker_seen = True
            break
        time.sleep(15)

    # If the pod script never printed 'TEST VIDEO FINISHED', it exited with an
    # error. Keep the pod so the user can debug; don't download an empty file.
    if not success_marker_seen:
        print("\n[!] Pod script did NOT reach 'TEST VIDEO FINISHED' -- run failed.")
        print("    NOT terminating the pod. Investigate then delete it in the console.")
        print(f"    ssh -i {SSH_KEY} -p {port} root@{host}")
        print(f"    tail -f {POD_LOG}")
        return

    if not output_path_remote:
        output_path_remote = f"/workspace/prediction_{args.model}_{args.duration}s.mp4"

    # Verify the file exists AND has content before downloading.
    chk = ssh_run(host, port,
                  f"if [ -s '{output_path_remote}' ]; then stat -c%s '{output_path_remote}'; "
                  f"else echo MISSING; fi",
                  quiet=True, check=False)
    chk_out = chk.stdout.decode("utf-8", "replace").strip() if chk else ""
    if "MISSING" in chk_out or not chk_out.isdigit() or int(chk_out) == 0:
        print(f"\n[!] Output file missing or empty on pod: {output_path_remote}")
        print("    NOT terminating the pod so you can SSH in.")
        print(f"    ssh -i {SSH_KEY} -p {port} root@{host}")
        return

    local_dir = os.path.join(REPO_ROOT, "trained_models")
    os.makedirs(local_dir, exist_ok=True)
    local_out = args.output or os.path.join(local_dir, os.path.basename(output_path_remote))

    print(f"\nDownloading ({int(chk_out) / (1024 * 1024):.1f} MB):\n"
          f"  {output_path_remote}\n  -> {local_out}")
    if download_from_pod(host, port, output_path_remote, local_out):
        size_mb = os.path.getsize(local_out) / (1024 * 1024)
        print(f"  saved: {local_out} ({size_mb:.1f} MB)")
    else:
        print(f"  [!] download failed. Still on pod at: {output_path_remote}")
        print(f"      ssh -i {SSH_KEY} -p {port} root@{host}")
        return

    if args.keep_pod:
        print(f"Leaving pod running (--keep-pod). Terminate manually when done.")
        print(f"SSH: ssh -i {SSH_KEY} -p {port} root@{host}")
    else:
        print(f"Terminating pod {pod_id} ...")
        terminate_pod_quiet(runpod, pod_id)
        print("Pod terminated. (Volume + files preserved.)")


def list_pod_segments(host, port, args):
    """--list-segments: enumerate segments visible on the volume so users can pick one."""
    print("Segments on the pod (with GT):")
    r = ssh_run(host, port,
                f"find {POD_DATA} -name gt_distill.h5 -printf '%h\\n' 2>/dev/null | sort | head -50",
                quiet=True, check=False)
    if r:
        print(r.stdout.decode("utf-8", "replace").rstrip() or "  (none)")
    print("\nSegments on the pod (any video, first 50):")
    r = ssh_run(host, port,
                f"find {POD_DATA} -name video.hevc -printf '%h\\n' 2>/dev/null | sort | head -50",
                quiet=True, check=False)
    if r:
        print(r.stdout.decode("utf-8", "replace").rstrip() or "  (none)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list GPUs + volumes and exit")
    ap.add_argument("--list-segments", action="store_true",
                    help="boot a pod, list segments on the volume, then terminate")
    ap.add_argument("--volume", default=VOLUME_ID)
    ap.add_argument("--gpu", default=GPU_CANDIDATES_TEST[0],
                    help="preferred GPU. Default is the first Ampere card in the "
                         "test-launcher's compatible list (see GPU_CANDIDATES_TEST).")
    ap.add_argument("--strict-gpu", action="store_true")
    ap.add_argument("--dc", default=DATA_CENTER)
    ap.add_argument("--boot-timeout", type=int, default=120)
    ap.add_argument("--boot-retries", type=int, default=5)
    ap.add_argument("--model", default="run1",
                    help="date_it of the trained model on the volume (i.e. <model>.pth)")
    ap.add_argument("--segment", default=None,
                    help="absolute path on the pod to a specific segment dir "
                         "(default: pick one automatically that has GT)")
    ap.add_argument("--duration", type=float, default=60.0, help="clip length in seconds")
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--output", default=None,
                    help="local output .mp4 path (default: trained_models/<remote-basename>)")
    ap.add_argument("--keep-pod", action="store_true",
                    help="don't terminate the pod after downloading (for debugging)")
    ap.add_argument("--reuse-pod", default=None,
                    help="reuse an already-running pod by ID (skips boot). Useful when a "
                         "prior run was interrupted and left a paid pod up.")
    ap.add_argument("--no-compare", action="store_true",
                    help="render ONLY the student model (skip the teacher side-by-side). "
                         "Default: render teacher (supercombo.onnx) | student side-by-side.")
    ap.add_argument("--calib-montage", type=int, default=None, metavar="FRAME_IDX",
                    help="DEBUG: render a calibration montage PNG at this frame (student "
                         "only, fast) instead of a video, to pick the right rpy visually.")
    ap.add_argument("--rpy-calib", default=None, metavar="R P Y",
                    help="manual calibration in DEGREES 'roll pitch yaw' (e.g. "
                         "\"0.4 2.1 2.1\"). Overrides the auto global_pose estimate. "
                         "Used both for the full render and as the montage's center.")
    ap.add_argument("--num-segments", type=int, default=1, metavar="N",
                    help="concatenate N randomly-chosen segments (each ~1 min) into one "
                         "video. Each gets its own calibration and a fresh recurrent "
                         "state at the cut. e.g. --num-segments 3 --duration 180.")
    ap.add_argument("--seed", type=int, default=42,
                    help="seed for the random segment pick (default 42, reproducible)")
    args = ap.parse_args()

    runpod = get_runpod()
    if args.list:
        list_resources()
        return
    if not args.volume:
        die("no network volume set. Run --list to find its ID, then pass --volume <ID>.")

    if args.reuse_pod:
        # Skip pod creation entirely; just wait for SSH on the existing pod.
        from runpod_lib import wait_for_ssh
        pod_id = args.reuse_pod
        res = wait_for_ssh(runpod, pod_id, timeout=args.boot_timeout)
        if res is None:
            die(f"pod {pod_id} did not expose SSH within {args.boot_timeout}s.")
        host, port = res
    else:
        # Use the Ampere-only test list (not the broader training list).
        try_gpus = [args.gpu] + [g for g in GPU_CANDIDATES_TEST if g != args.gpu]
        if args.strict_gpu:
            try_gpus = [args.gpu]
        pod_id, host, port = boot_pod_with_retries(
            runpod, name=f"optest-{args.model}", dc=args.dc, volume_id=args.volume,
            gpu_candidates=try_gpus, boot_timeout=args.boot_timeout, boot_retries=args.boot_retries)

    if args.list_segments:
        list_pod_segments(host, port, args)
        print(f"\nTerminating pod {pod_id} ...")
        terminate_pod_quiet(runpod, pod_id)
        return

    try:
        start_remote(host, port, args)

        print("\n=========================================================")
        print(f" Pod:    {pod_id}")
        print(f" SSH:    ssh -i {SSH_KEY} -p {port} root@{host}")
        print(f" Log:    tail -f {POD_LOG}")
        print(" This will download the mp4 to ./trained_models/ then terminate the pod.")
        print("=========================================================")
        wait_download_terminate(host, port, pod_id, runpod, args)
    except KeyboardInterrupt:
        print("\nInterrupted. Pod is still running -- terminate it in the console to stop billing.")
        print(f"  SSH: ssh -i {SSH_KEY} -p {port} root@{host}")
        sys.exit(130)


if __name__ == "__main__":
    main()
