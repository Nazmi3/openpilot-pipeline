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
python launch_runpod_training.py --date-it run1 --epochs 15 --wait --snpe-sdk-url <url>  # + auto-convert to .dlc
python launch_runpod_training.py --date-it run1 --convert-only --wait     # convert existing .pth -> .onnx/.dlc, no retrain
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

# Shared RunPod plumbing (config, ssh, pod lifecycle, downloads).
# Anything not training-specific lives in runpod_lib so the test-video
# launcher can use the exact same code path.
from runpod_lib import (
    VOLUME_ID, DATA_CENTER, GPU_TYPE, GPU_CANDIDATES, IMAGE, CONTAINER_DISK_GB, MOUNT_PATH,
    WANDB_ENTITY, WANDB_PROJECT, SSH_KEY, API_KEY, WANDB_KEY,
    die, get_runpod, list_resources, ssh_run, wait_for_ssh, download_from_pod,
    sync_local_files_to_pod,
    tail_lines_from as _tail_lines_from,
    boot_pod_with_retries, terminate_pod_quiet,
)

SNPE_SDK_URL  = os.environ.get("SNPE_SDK_URL",
    "https://softwarecenter.qualcomm.com/api/download/software/sdks/"
    "Qualcomm_AI_Runtime_Community/All/2.48.0.260626/v2.48.0.260626.zip")
# SNPE 1.x zips are login-gated on developer.qualcomm.com, so there is no usable
# default -- pass --snpe-sdk-url (or set SNPE1_SDK_URL) when using --snpe-major 1.
SNPE1_SDK_URL = os.environ.get("SNPE1_SDK_URL", "")

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
: "${LOG_FREQ:=10}" "${VAL_FREQ:=20}" "${CONVERT_ONLY:=0}" "${SPLIT:=0.75}" "${GEN_GT_COUNT:=0}" "${GT_ONLY:=0}"
: "${MHP_LOSS:=0}" "${REINIT_HEAD:=0}" "${GRAD_CLIP:=}"
: "${WB_ENTITY:=nazmiryuki}" "${WB_PROJECT:=openpilot-pipeline}"
: "${WANDB_API_KEY:=}" "${RUNPOD_API_KEY:=}" "${SNPE_SDK_URL:=}" "${SNPE_MAJOR:=2}"

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

# 6b) patch train.py: the wandb preview render builds two full-segment RGB videos in
#     RAM (~1GB each) and OOM-kills training on memory-limited pods -- and because it
#     runs in the final epoch BEFORE the end-of-training model save, an OOM there means
#     the trained model never gets saved/converted. So gate the render behind
#     RENDER_PREVIEW (default off) AND keep it to the final epoch only. Idempotent;
#     upgrades the older VIZ_AT_END patch. Survives 'git checkout $PIN' (refuses to
#     clobber local modifications).
python - "$REPO/train/train.py" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
orig     = "                visualize_predictions(model, device, train_segment_for_viz, val_segment_for_viz)\n"
old_viz  = ("                visualize_predictions(model, device, train_segment_for_viz, val_segment_for_viz)"
            " if epoch == epochs - 1 else None  # VIZ_AT_END\n")
new_line = ("                visualize_predictions(model, device, train_segment_for_viz, val_segment_for_viz)"
            " if (epoch == epochs - 1 and os.environ.get('RENDER_PREVIEW','0') == '1') else None  # VIZ_AT_END RENDER_PREVIEW\n")
if 'RENDER_PREVIEW' in s:
    print(">>> train.py already patched (RENDER_PREVIEW)")
elif old_viz in s:
    open(p, 'w', encoding='utf-8').write(s.replace(old_viz, new_line, 1))
    print(">>> upgraded train.py viz patch: preview gated on RENDER_PREVIEW (final epoch only)")
elif orig in s:
    open(p, 'w', encoding='utf-8').write(s.replace(orig, new_line, 1))
    print(">>> patched train.py: preview gated on RENDER_PREVIEW (final epoch only)")
else:
    print(">>> [warn] could not patch train.py viz (call not found); viz unchanged")
PYEOF

# 6c) make the preview render memory-safe. The stock render loads a full ~1190-frame
#     segment (a ~1.8GB YUV buffer + two ~1GB RGB video arrays) and OOM-kills training on
#     small pods -- and since it runs in the final epoch before the model save, that also
#     loses the trained model. So (1) cap the rendered frames (VIZ_FRAMES, default 450)
#     and (2) bound the GT loop to the rendered length. The preview still uploads to wandb
#     exactly as before; we just don't buffer the whole segment. Idempotent (VIZ_CAP marker).
python - "$REPO/train/train.py" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
if 'VIZ_CAP' in s:
    print(">>> train.py already patched (VIZ_CAP)"); raise SystemExit
load_old = "            input_frames, rgb_frames = load_transformed_video(path_to_segment)\n"
load_new = "            input_frames, rgb_frames = load_transformed_video(path_to_segment, seq_len=int(os.environ.get('VIZ_FRAMES', '450')))  # VIZ_CAP\n"
gt_old = "            for k in range(plan_gt_h5.shape[0]):\n"
gt_new = "            for k in range(min(plan_gt_h5.shape[0], rgb_frames.shape[0])):\n"
n = 0
for old, new in [(load_old, load_new), (gt_old, gt_new)]:
    if old in s:
        s = s.replace(old, new, 1); n += 1
    else:
        print(">>> [warn] VIZ_CAP: pattern not found:\n" + old.strip())
open(p, 'w', encoding='utf-8').write(s)
print(">>> patched train.py: capped preview to VIZ_FRAMES frames (%d/2 edits)" % n)
PYEOF

# 6d) OPTION-1 calibration: project the preview with each segment's real liveCalibration
#     rpy instead of a hardcoded [0,0,0], so the GT/pred paths lie flat on the road instead
#     of pitching down. Reuses gt_distill.parse_logs (LogReader). Robust: falls back to zero
#     rpy with a warning if a segment's log can't be parsed. Idempotent (VIZ_CALIB marker).
python - "$REPO/train/train.py" <<'PYEOF'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
if 'VIZ_CALIB' in s:
    print(">>> train.py already patched (VIZ_CALIB)"); raise SystemExit

# built from double-quoted line pieces on purpose: a triple-quoted Python string here
# would terminate the outer PROVISION_SCRIPT literal that this whole script lives in.
helper = "\n".join([
    "_VIZ_RPY = [0, 0, 0]  # VIZ_CALIB",
    "",
    "",
    "def _set_viz_rpy(segment_path):",
    "    # mean liveCalibration rpy so the preview projection matches real camera roll/pitch/yaw",
    "    global _VIZ_RPY",
    "    try:",
    "        import numpy as _np, os as _os, sys as _sys",
    "        _op = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), '..', 'common'))",
    "        if _op not in _sys.path:",
    "            _sys.path.append(_op)",
    "        from gt_distill.parse_logs import parse_logs",
    "        rpy_seg, _ext = parse_logs(segment_path, _op)",
    "        if rpy_seg is None:",
    "            printf('[viz] no calibration for ' + str(segment_path) + '; using zero rpy')",
    "            _VIZ_RPY = [0, 0, 0]; return",
    "        rpy = _np.asarray(rpy_seg).reshape(-1, 3)",
    "        rpy = rpy[_np.any(rpy != 0, axis=1)]",
    "        _VIZ_RPY = [0, 0, 0] if rpy.shape[0] == 0 else [float(v) for v in rpy.mean(axis=0)]",
    "        printf('[viz] segment rpy (roll,pitch,yaw) = ' + str(_VIZ_RPY))",
    "    except Exception as _e:",
    "        printf('[viz] calibration parse failed (' + str(_e) + '); using zero rpy')",
    "        _VIZ_RPY = [0, 0, 0]",
    "",
    "",
    "",
])

anchor_def = "def visualization(lanelines, roadedges, calib_path, im_rgb):\n"
hard = "    rpy_calib = [0, 0, 0]\n"
soft = "    rpy_calib = _VIZ_RPY  # VIZ_CALIB: per-segment liveCalibration\n"
viz_old = ('            path_to_segment = segments_for_viz[i]\n'
           '            printf(f"===>Visualizing predictions: {path_to_segment}")\n')
viz_new = viz_old + '            _set_viz_rpy(path_to_segment)\n'

n = 0
if anchor_def in s:
    s = s.replace(anchor_def, helper + anchor_def, 1); n += 1
else:
    print(">>> [warn] VIZ_CALIB: visualization() def not found")
if hard in s:
    s = s.replace(hard, soft, 1); n += 1
else:
    print(">>> [warn] VIZ_CALIB: rpy_calib=[0,0,0] line not found")
if viz_old in s:
    s = s.replace(viz_old, viz_new, 1); n += 1
else:
    print(">>> [warn] VIZ_CALIB: visualize_predictions anchor not found")
open(p, 'w', encoding='utf-8').write(s)
print(">>> patched train.py: preview uses per-segment liveCalibration rpy (%d/3 edits)" % n)
PYEOF

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

# 9b) generate GT for a LIMITED number of additional segments (grow the training set
#     without processing all ~2000). Picks only segments that don't already have GT,
#     so it's safe to re-run and it never redoes existing work.
if [ "${GEN_GT_COUNT:-0}" -gt 0 ]; then
  cd "$REPO"
  HAVE=$(find "$DATA" -name gt_distill.h5 2>/dev/null | wc -l)
  echo ">>> GT grow: have $HAVE segments, generating up to $GEN_GT_COUNT more"
  # segments (dirs with video.hevc) that do NOT yet have gt_distill.h5
  find "$DATA" -name video.hevc -printf '%h\n' 2>/dev/null | sort -u | while read -r d; do
    [ -f "$d/gt_distill.h5" ] || printf '%s\n' "$d"
  done > "$WS/gt_todo_all.txt"
  head -n "$GEN_GT_COUNT" "$WS/gt_todo_all.txt" > "$WS/gt_todo.txt"
  NTODO=$(wc -l < "$WS/gt_todo.txt")
  echo ">>> $NTODO new segments to process"
  if [ "$NTODO" -gt 0 ]; then
    # Keep parallelism modest: each worker loads the model + decodes ~1200 frames,
    # so too many at once OOM-kills some (that's what capped a 16-wide run at ~43%).
    # Tunable via GT_PAR (concurrent workers) and GT_THREADS_EACH.
    N="${GT_PAR:-4}"; T="${GT_THREADS_EACH:-6}"; SD="$WS/gt_shards"
    mkdir -p "$SD" "$WS/gt_logs"; rm -f "$SD"/s_*.txt "$WS/gt_logs"/grow_*.log
    awk -v n=$N -v d="$SD" '{print > (d "/s_" (NR%n) ".txt")}' "$WS/gt_todo.txt"
    for i in $(seq 0 $((N-1))); do
      [ -s "$SD/s_$i.txt" ] || continue
      GT_THREADS=$T OMP_NUM_THREADS=$T python gt_distill/generate_gt.py \
        --recordings_basedir "$DATA" --cache "$SD/s_$i.txt" \
        --openpilot_dir "$REPO/common" > "$WS/gt_logs/grow_$i.log" 2>&1 &
    done
    wait
    # surface why any segments failed (OOM kill vs bad/missing data vs code error)
    echo ">>> grow failure summary (top messages, if any):"
    grep -hiE 'error|exception|killed|traceback|no such|cannot|memoryerror' \
      "$WS/gt_logs"/grow_*.log 2>/dev/null | sed 's/[0-9]\{2,\}//g' \
      | sort | uniq -c | sort -rn | head -10 || true
  fi
  echo ">>> GT grow done: $(find "$DATA" -name gt_distill.h5 | wc -l) segments total"
  # The dataset caches its segment list in $REPO/cache/{segments,videos,plans}.txt and
  # reuses it without rescanning. We just changed the GT set, so invalidate that cache;
  # the next training run rebuilds it (once) and later runs reuse it. Clearing only here
  # (not every train run) avoids a slow full rescan of the 94GB tree on each run.
  rm -f "$REPO/cache/segments.txt" "$REPO/cache/videos.txt" "$REPO/cache/plans.txt" 2>/dev/null || true
  echo ">>> invalidated dataset path cache (next training run will rescan)"
fi

# 10) train. train.py sometimes hangs on exit (dataloader/wandb threads don't
#     close), which would block auto-stop / the launcher forever. So run it in
#     the background and force-exit once it logs "training_finished" (the model
#     is already saved by that point). Skipped entirely in CONVERT_ONLY mode
#     (which just re-converts an already-trained .pth on the volume).
if [ "$GT_ONLY" = "1" ]; then
  echo ">>> GT_ONLY=1: grew the dataset, skipping training + conversion"
elif [ "$CONVERT_ONLY" = "1" ]; then
  echo ">>> CONVERT_ONLY=1: skipping training, will convert existing .pth"
else
  # NOTE: the dataset path cache is invalidated in the GT-grow step (9b) when the GT set
  # changes, so it's correct here without a slow rescan every run.
  echo ">>> starting training ($DATE_IT) $(date)"
  cd "$REPO/train"
  TLOG="$WS/train_${DATE_IT}.log"
  # loss selection: default is KL-divergence distillation; --mhp-loss switches to
  # the sigma-clamped Laplacian likelihood loss (train.py's --mhp_loss).
  MHP_FLAG=""
  if [ "$MHP_LOSS" = "1" ]; then MHP_FLAG="--mhp_loss"; echo ">>> using Laplacian MHP likelihood loss"; fi
  if [ -z "$MHP_FLAG" ]; then echo ">>> using KL-divergence distillation loss (full teacher supervision)"; fi
  # head init: default warm-starts the path-plan head from the teacher's weights
  REINIT_FLAG=""
  if [ "$REINIT_HEAD" = "1" ]; then REINIT_FLAG="--reinit_head"; echo ">>> re-initializing path-plan head from scratch"; fi
  # gradient clipping (train.py defaults to inf == disabled)
  CLIP_FLAG=""
  if [ -n "${GRAD_CLIP:-}" ]; then CLIP_FLAG="--grad_clip $GRAD_CLIP"; echo ">>> grad clip: $GRAD_CLIP"; fi
  # WANDB_START_METHOD=thread: run wandb in a thread instead of a helper subprocess.
  # The subprocess default intermittently fails with "Error communicating with wandb
  # process" on resource-constrained pods, which crashes train.py before any epoch.
  PYTHONPATH="$REPO" WANDB_ENTITY="$WB_ENTITY" WANDB_PROJECT="$WB_PROJECT" \
    WANDB_START_METHOD=thread \
    python train.py --date_it "$DATE_IT" --recordings_basedir "$DATA" \
    --batch_size "$BATCH" --epochs "$EPOCHS" --split "$SPLIT" \
    --log_frequency "$LOG_FREQ" --val_frequency "$VAL_FREQ" \
    $MHP_FLAG $REINIT_FLAG $CLIP_FLAG > "$TLOG" 2>&1 &
  TRAIN_PID=$!
  while kill -0 "$TRAIN_PID" 2>/dev/null; do
    if grep -q "training_finished" "$TLOG" 2>/dev/null; then
      sleep 10; kill "$TRAIN_PID" 2>/dev/null; pkill -P "$TRAIN_PID" 2>/dev/null; break
    fi
    sleep 15
  done
  wait "$TRAIN_PID" 2>/dev/null
  echo ">>> TRAINING FINISHED $(date)"
fi

# 11) convert .pth -> .onnx -> .dlc  (skipped in GT_ONLY mode -- nothing was trained)
TRAIN_DIR="$REPO/train"
MODEL_PTH="$TRAIN_DIR/nets/model_itr/${DATE_IT}.pth"
MODEL_ONNX="$TRAIN_DIR/nets/model_itr/${DATE_IT}.onnx"
MODEL_DLC="$TRAIN_DIR/nets/model_itr/${DATE_IT}.dlc"

if [ "$GT_ONLY" = "1" ]; then
  echo ">>> GT_ONLY=1: skipping .pth -> .onnx -> .dlc conversion"
elif [ -f "$MODEL_PTH" ]; then
  echo ">>> converting .pth -> .onnx"
  conda activate "$ENVN"
  cd "$TRAIN_DIR"
  PYTHONPATH="$REPO" python torch_to_onnx.py "$MODEL_PTH" "$MODEL_ONNX" \
    && echo ">>> ONNX conversion done: $MODEL_ONNX" \
    || echo ">>> [warn] ONNX conversion failed"
else
  echo ">>> [warn] .pth not found, skipping conversion: $MODEL_PTH"
fi

if [ "$GT_ONLY" != "1" ] && [ -f "$MODEL_ONNX" ] && [ -n "$SNPE_SDK_URL" ]; then
  # SNPE_MAJOR picks the converter generation, and they are NOT interchangeable:
  #   2 = QAIRT / SNPE 2.x -> zip root qairt/<ver>/, converter needs python3.10
  #   1 = SNPE 1.x         -> zip root snpe-<ver>/,  converter needs python3.6
  # bukapilot (openpilot 0.8.13) links the SNPE 1.x C++ API (namespace zdl, see
  # ../bukapilot/third_party/snpe/include/), whose runtime cannot load a DLC written
  # by the 2.x converter. A .dlc meant for the device must be built with SNPE_MAJOR=1.
  # Separate cache dirs so both SDKs can coexist on the volume.
  if [ "$SNPE_MAJOR" = "1" ]; then SNPE_DIR="$WS/snpe_sdk1"; else SNPE_DIR="$WS/snpe_sdk"; fi
  echo ">>> setting up SNPE ${SNPE_MAJOR}.x SDK for .onnx -> .dlc ($SNPE_DIR)"

  find_sdk_root() {
    if [ "$SNPE_MAJOR" = "1" ]; then
      # 1.x zips unpack as snpe-<version>/ at the root
      find "$SNPE_DIR" -mindepth 1 -maxdepth 1 -type d -name 'snpe-*' 2>/dev/null | head -1
    else
      # 2.x zips unpack as qairt/<version>/
      find "$SNPE_DIR/qairt" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1
    fi
  }
  SDK_ROOT=$(find_sdk_root)
  if [ -z "$SDK_ROOT" ]; then
    mkdir -p "$SNPE_DIR"
    echo ">>> downloading SNPE ${SNPE_MAJOR}.x SDK (one-time; this can take several minutes)..."
    curl -fsSL -o /tmp/snpe_sdk.zip "$SNPE_SDK_URL"
    echo ">>> unpacking SDK..."
    unzip -q /tmp/snpe_sdk.zip -d "$SNPE_DIR"; rm -f /tmp/snpe_sdk.zip
    SDK_ROOT=$(find_sdk_root)
    [ -n "$SDK_ROOT" ] && echo ">>> SDK ready: $SDK_ROOT"
  fi
  SNPE_BIN="${SDK_ROOT:-}/bin/x86_64-linux-clang/snpe-onnx-to-dlc"

  if [ -n "$SDK_ROOT" ] && [ -f "$SNPE_BIN" ]; then
    # libc++1/libc++abi1: the converter's compiled extension is built with
    # clang/libc++ (the base image only ships libstdc++, so libc++.so.1 is
    # otherwise missing). apt packages live in the EPHEMERAL container root, not
    # the network volume, so this must run on EVERY pod boot.
    apt-get install -y -qq libc++1 libc++abi1 >/dev/null 2>&1 || true
    export SNPE_ROOT="$SDK_ROOT"
    CONV_ONNX="$MODEL_ONNX"
    RUN_CONV=0

    if [ "$SNPE_MAJOR" = "1" ]; then
      # SNPE 1.x's converter is a python3.6 script and Ubuntu 22.04 has no
      # python3.6 package -- build the interpreter with conda instead. Kept in its
      # own env so it can't disturb the training env.
      echo ">>> ensuring python3.6 conda env for the SNPE 1.x converter"
      conda activate base
      if [ ! -d "$CONDA/envs/snpe1" ]; then
        conda create -y -q -n snpe1 python=3.6 >/dev/null 2>&1 \
          || echo ">>> [warn] failed to create the snpe1 conda env"
      fi
      if conda activate snpe1 2>/dev/null; then
        # Versions the 1.x converter was validated against. onnx>1.8 emits IR
        # versions its protobuf schema can't parse; numpy>=1.20 breaks its C ABI.
        python -c "import onnx, numpy" 2>/dev/null \
          || pip install -q "onnx==1.8.1" "numpy==1.19.5" "protobuf==3.17.3" pyyaml \
          || echo ">>> [warn] some SNPE 1.x python deps failed to install"
        # libpython3.6m.so lives in the conda env; the converter's extension
        # dlopen()s it by name, so its dir has to be on LD_LIBRARY_PATH.
        LIBPY_DIR="$CONDA/envs/snpe1/lib"
        # torch exports IR version 7; the 1.x converter's bundled onnx schema tops
        # out at 6. The opset-9 graph itself is unchanged by the downgrade, so
        # clamping the field is safe and avoids a re-export.
        CONV_ONNX="${MODEL_ONNX%.onnx}_ir6.onnx"
        python - "$MODEL_ONNX" "$CONV_ONNX" <<'PY' || CONV_ONNX="$MODEL_ONNX"
import sys, onnx
m = onnx.load(sys.argv[1])
if m.ir_version > 6:
    print('>>> clamping ONNX ir_version %d -> 6 for the SNPE 1.x converter' % m.ir_version)
    m.ir_version = 6
onnx.save(m, sys.argv[2])
PY
        RUN_CONV=1
      fi
    else
      # snpe-onnx-to-dlc is a python script (qti.aisw.converters) that needs Python
      # 3.10 on Ubuntu 22.04 with pinned deps, kept in its OWN venv (must not pollute
      # the conda training env, whose numpy/onnx/etc. versions differ).
      SNPE_VENV="$WS/snpe_venv"
      # The converter's compiled extension (libPyIrGraph310.so) dlopen()s
      # libpython3.10.so.1.0. apt packages live in the EPHEMERAL container root (NOT
      # the network volume), so python3.10 + libpython3.10 must be (re)installed on
      # EVERY pod boot -- even when the venv already exists on the volume.
      echo ">>> ensuring python3.10 + libpython3.10 present (needed by the converter)"
      apt-get install -y -qq python3.10 python3.10-venv python3-distutils libpython3.10 \
        >/dev/null 2>&1 || true
      # Fallback: locate libpython3.10.so.1.0 so we can add its dir to LD_LIBRARY_PATH
      # in case apt put it somewhere off the default loader path.
      LIBPY=$(find /usr/lib /usr/local/lib /opt -name 'libpython3.10.so*' 2>/dev/null | head -1)
      LIBPY_DIR=""; [ -n "$LIBPY" ] && LIBPY_DIR=$(dirname "$LIBPY")
      if [ ! -x "$SNPE_VENV/bin/python" ]; then
        echo ">>> creating dedicated python3.10 venv for the SNPE converter"
        python3.10 -m venv "$SNPE_VENV" --without-pip 2>/dev/null \
          || echo ">>> [warn] failed to create SNPE venv (python3.10 missing?)"
        "$SNPE_VENV/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
      fi
      if [ -x "$SNPE_VENV/bin/python" ]; then
        # shellcheck disable=SC1091
        source "$SNPE_VENV/bin/activate"
        if ! PYTHONPATH="$SDK_ROOT/lib/python:${PYTHONPATH:-}" python -c "import onnx, qti.aisw" 2>/dev/null; then
          echo ">>> installing SNPE converter python deps (one-time)"
          python -m pip install -q --upgrade pip
          python -m pip install -q "onnx==1.19.1"
          PYTHONPATH="$SDK_ROOT/lib/python:${PYTHONPATH:-}" \
            python "$SDK_ROOT/bin/check-python-dependency" \
            || echo ">>> [warn] some SNPE python deps failed to install"
        fi
        # Pin numpy to the version Qualcomm's compiled extension expects (numpy 2.x
        # breaks its C ABI). Enforced every run since the guard above may skip reinstall.
        python -c "import numpy; assert numpy.__version__ == '1.26.4'" 2>/dev/null \
          || python -m pip install -q "numpy==1.26.4"
        RUN_CONV=1
      fi
    fi

    if [ "$RUN_CONV" = "1" ]; then
      echo ">>> running snpe-onnx-to-dlc (SNPE ${SNPE_MAJOR}.x)..."
      # Drop any DLC left by a previous run (possibly from the OTHER SNPE major),
      # so the success check below can't be fooled by a stale file.
      rm -f "$MODEL_DLC"
      # The ONNX is exported with a dynamic batch axis, so SNPE needs each input's
      # concrete shape via -d. Batch is fixed to 1 (openpilot runs supercombo at
      # batch 1 on-device). Shapes come from train/torch_to_onnx.py's example inputs.
      run_converter() {
        PYTHONPATH="$SDK_ROOT/lib/python:${PYTHONPATH:-}" \
          LD_LIBRARY_PATH="$SDK_ROOT/lib/x86_64-linux-clang:${LIBPY_DIR:+$LIBPY_DIR:}${LD_LIBRARY_PATH:-}" \
          python "$SNPE_BIN" "$1" "$CONV_ONNX" "$2" "$MODEL_DLC" \
          -d input_imgs 1,12,128,256 \
          -d desire 1,8 \
          -d traffic_convention 1,2 \
          -d initial_state 1,512
      }
      # Flag names drifted across SNPE releases: 2.x and late 1.x take
      # --input_network/--output_path, mid 1.x --model_path/--output_path, and
      # early 1.x --model_path/--dlc. Try newest first and fall back.
      run_converter --input_network --output_path \
        || run_converter --model_path --output_path \
        || run_converter --model_path --dlc \
        || true
      if [ -f "$MODEL_DLC" ]; then
        echo ">>> DLC conversion done: $MODEL_DLC ($(stat -c%s "$MODEL_DLC") bytes)"
      else
        echo ">>> [warn] DLC conversion failed"
      fi
      [ "$CONV_ONNX" != "$MODEL_ONNX" ] && rm -f "$CONV_ONNX"
      # venv uses `deactivate`, the 1.x conda env uses `conda deactivate`
      deactivate 2>/dev/null || conda deactivate 2>/dev/null || true
    fi
  else
    echo ">>> [warn] snpe-onnx-to-dlc not found under $SNPE_DIR (bad SDK zip layout?)"
  fi
elif [ -f "$MODEL_ONNX" ]; then
  echo ">>> [info] SNPE_SDK_URL not set, skipping .dlc conversion"
fi

# Final marker: printed ONLY after train + conversion are fully done. The --wait
# launcher polls for this exact string, so it never downloads/terminates until
# the .onnx/.dlc actually exist. (Do NOT key completion off 'training_finished';
# that is logged before conversion runs.)
echo ">>> PIPELINE FINISHED $(date)"

# 12) optional: stop the pod to end GPU billing
if [ "$AUTO_STOP" = "1" ] && [ -n "${RUNPOD_POD_ID:-}" ]; then
  echo ">>> auto-stopping pod $RUNPOD_POD_ID"
  curl -s -X POST "https://rest.runpod.io/v1/pods/$RUNPOD_POD_ID/stop" \
    -H "Authorization: Bearer $RUNPOD_API_KEY" || runpodctl stop pod "$RUNPOD_POD_ID" || true
fi
'''


# --------------------------------------------------------------------------------
# NOTE: die / get_runpod / list_resources / ssh_run / wait_for_ssh /
# download_from_pod / _tail_lines_from are imported from runpod_lib above.
# Only training-specific glue lives below.
# --------------------------------------------------------------------------------


# Local files overlaid onto the pod's repo copy before training.
#
# The pod clones nikebless/openpilot-pipeline pinned at $PIN, so WITHOUT this
# sync the pod trains with upstream code and any local change (new CLI flags,
# loss/init changes) is silently ignored -- or crashes argparse. The provision
# script's `git checkout $PIN` keeps these local modifications.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
POD_REPO = "/workspace/openpilot-pipeline"
FILES_TO_SYNC = [
    ("utils.py",            f"{POD_REPO}/utils.py"),
    ("train/train.py",      f"{POD_REPO}/train/train.py"),
    ("train/model.py",      f"{POD_REPO}/train/model.py"),
    ("train/dataloader.py", f"{POD_REPO}/train/dataloader.py"),
    ("train/torch_to_onnx.py", f"{POD_REPO}/train/torch_to_onnx.py"),
    ("gt_distill/generate_gt.py", f"{POD_REPO}/gt_distill/generate_gt.py"),
    ("gt_distill/parse_logs.py", f"{POD_REPO}/gt_distill/parse_logs.py"),
]


def start_remote(host, port, args):
    # remove stale logs from a prior run with the same date_it BEFORE launching,
    # so the --wait poll can't see an old 'PIPELINE FINISHED' from a previous run.
    ssh_run(host, port,
            f"rm -f /workspace/train_{args.date_it}.log /workspace/launch_{args.date_it}.log",
            quiet=True, check=False)
    # overlay local code onto the pod's clone (see FILES_TO_SYNC)
    print("Syncing locally modified files to the pod's repo copy...")
    if sync_local_files_to_pod(host, port, FILES_TO_SYNC, REPO_ROOT) == 0:
        die("no local files were uploaded -- check the paths in FILES_TO_SYNC.")
    # upload the self-contained provisioning script...
    print("Uploading provisioning script to the pod...")
    if ssh_run(host, port, "cat > /workspace/_provision.sh && chmod +x /workspace/_provision.sh",
               stdin=PROVISION_SCRIPT) is None:
        die("failed to upload provisioning script over SSH")
    # ...and run it fully detached with all parameters passed via env.
    snpe_major = str(getattr(args, "snpe_major", 2))
    # a 2.x URL is useless to the 1.x converter, so don't silently fall back to it
    snpe_url = (getattr(args, "snpe_sdk_url", None)
                or (SNPE1_SDK_URL if snpe_major == "1" else SNPE_SDK_URL))
    if snpe_major == "1" and not snpe_url:
        die("--snpe-major 1 needs an SNPE 1.x SDK zip URL: pass --snpe-sdk-url <url> "
            "or set SNPE1_SDK_URL (the 1.x SDK is login-gated on developer.qualcomm.com).")
    envs = {
        "DATE_IT": args.date_it, "EPOCHS": str(args.epochs), "BATCH": str(args.batch_size),
        "GEN_GT": "1" if args.gen_gt else "0",
        "CONVERT_ONLY": "1" if args.convert_only else "0",
        # in --wait mode the launcher downloads the model then terminates the pod,
        # so the pod must NOT stop itself first.
        "AUTO_STOP": "1" if (args.auto_stop and not args.wait) else "0",
        "LOG_FREQ": str(args.log_frequency), "VAL_FREQ": str(args.val_frequency),
        "SPLIT": str(args.split), "GEN_GT_COUNT": str(args.gen_gt_count),
        "MHP_LOSS": "1" if args.mhp_loss else "0",
        "REINIT_HEAD": "1" if args.reinit_head else "0",
        "GRAD_CLIP": str(args.grad_clip) if args.grad_clip is not None else "",
        "RENDER_PREVIEW": "1" if args.preview else "0",
        "GT_ONLY": "1" if args.gt_only else "0",
        "WB_ENTITY": WANDB_ENTITY, "WB_PROJECT": WANDB_PROJECT,
        "WANDB_API_KEY": WANDB_KEY, "RUNPOD_API_KEY": API_KEY,
        "SNPE_SDK_URL": snpe_url, "SNPE_MAJOR": snpe_major,
    }
    env_prefix = " ".join(f'{k}="{v}"' for k, v in envs.items())
    launch = (f"setsid nohup env {env_prefix} bash /workspace/_provision.sh "
              f"> /workspace/launch_{args.date_it}.log 2>&1 < /dev/null & echo LAUNCHED pid=$!")
    print("Launching convert-only on the pod..." if args.convert_only
          else "Launching provision+train on the pod...")
    ssh_run(host, port, f"bash -lc '{launch}'")


def wait_download_terminate(host, port, pod_id, runpod, args):
    """Stream progress until training+conversion finishes, download model artifacts
    to ./trained_models/, then terminate the pod. Files remain on the volume so a
    failed download can be retried later with another pod."""
    log = f"/workspace/launch_{args.date_it}.log"
    tlog = f"/workspace/train_{args.date_it}.log"
    train_dir = "/workspace/openpilot-pipeline/train/nets"
    remote_model = f"{train_dir}/model_itr/{args.date_it}.pth"

    print("\n--wait: streaming progress until training+conversion finishes.")
    print("Keep this window open. Ctrl+C stops watching but NOT the pod.\n")
    next_launch_line = 1
    last_train_progress = ""

    def pump_launch_log():
        nonlocal next_launch_line
        new_lines, next_launch_line = _tail_lines_from(host, port, log, next_launch_line)
        for ln in new_lines:
            s = ln.rstrip("\r")
            if s.strip():
                print("  " + s)

    while True:
        # 1) stream provisioning + conversion markers ('>>> ...') and setup output.
        pump_launch_log()

        # 2) surface the latest training epoch/step progress line (filtering tqdm noise).
        r = ssh_run(host, port,
                    f"grep -aE 'Epoch [0-9]+/|Epoch [0-9]+ done|Validation Loss|Visualizing predictions' "
                    f"{tlog} 2>/dev/null | tail -1", quiet=True, check=False)
        tp = (r.stdout.decode("utf-8", "replace").strip() if r else "")
        if tp and tp != last_train_progress:
            last_train_progress = tp
            print("  [train] " + tp)

        # 3) done? 'PIPELINE FINISHED' is logged only after train AND conversion
        #    complete, so we never grab the .pth / terminate before .onnx/.dlc exist.
        #    The pgrep fallback catches a crashed/exited provision script.
        r = ssh_run(host, port,
                    f"if grep -q 'PIPELINE FINISHED' {log} 2>/dev/null || "
                    f"! pgrep -f '[_]provision.sh' >/dev/null 2>&1; then echo DONE; else echo RUNNING; fi",
                    quiet=True, check=False)
        state = r.stdout.decode("utf-8", "replace") if r else ""
        if "DONE" in state:
            pump_launch_log()  # flush any final lines (e.g. the PIPELINE FINISHED marker)
            break
        time.sleep(20)
    print("\nTraining + conversion finished (or the run ended).")

    # Locate the .pth: prefer the final model, else the newest checkpoint.
    find = ssh_run(host, port,
                   f"if [ -f {remote_model} ]; then echo FINAL {remote_model}; "
                   f"else ls -t {train_dir}/checkpoints/*.pth 2>/dev/null | head -1 | sed 's/^/CKPT /'; fi",
                   quiet=True, check=False)
    line = (find.stdout.decode("utf-8", "replace").strip() if find else "")
    local_dir = os.path.join(os.getcwd(), "trained_models")
    os.makedirs(local_dir, exist_ok=True)

    pth_remote = None
    if line.startswith("FINAL ") or line.startswith("CKPT "):
        kind, pth_remote = line.split(" ", 1)
        pth_remote = pth_remote.strip()
        local_pth = os.path.join(local_dir, f"{args.date_it}.pth" if kind == "FINAL"
                                  else os.path.basename(pth_remote))
        print(f"Downloading {kind} model:\n  {pth_remote}\n  -> {local_pth}")
        if download_from_pod(host, port, pth_remote, local_pth):
            print(f"  saved: {local_pth} ({os.path.getsize(local_pth)//(1024*1024)} MB)")
        else:
            print("  [!] download failed. Model is still on the volume at:")
            print(f"      {pth_remote}  (fetch it later with another pod)")
    else:
        print("[!] No model file found — training may have crashed. "
              "Check the wandb run / pod logs. Nothing to download.")

    # Download .onnx if present (produced by the post-training conversion step).
    stem = os.path.splitext(os.path.basename(pth_remote))[0] if pth_remote else args.date_it
    model_dir_remote = f"{train_dir}/model_itr"
    for ext in ("onnx", "dlc"):
        remote_path = f"{model_dir_remote}/{stem}.{ext}"
        chk = ssh_run(host, port, f"test -f '{remote_path}' && echo EXISTS || echo MISSING",
                      quiet=True, check=False)
        if chk and "EXISTS" in chk.stdout.decode("utf-8", "replace"):
            local_path = os.path.join(local_dir, f"{stem}.{ext}")
            print(f"Downloading .{ext}:\n  {remote_path}\n  -> {local_path}")
            if download_from_pod(host, port, remote_path, local_path):
                print(f"  saved: {local_path} ({os.path.getsize(local_path)//(1024*1024)} MB)")
            else:
                print(f"  [!] .{ext} download failed. Still on volume at: {remote_path}")
        else:
            print(f"  [info] .{ext} not found on pod (conversion may have been skipped).")

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
    ap.add_argument("--boot-timeout", type=int, default=120,
                    help="seconds to wait for a new pod to expose SSH before giving up on that "
                         "host and creating a fresh one (default 120)")
    ap.add_argument("--boot-retries", type=int, default=5,
                    help="how many pods to try if hosts get stuck 'Initializing' before failing "
                         "(default 5)")
    ap.add_argument("--dc", default=DATA_CENTER, help="data center id (must match the volume)")
    ap.add_argument("--date-it", default="run1", help="wandb run name")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch-size", type=int, default=8,
                    help="batch size (== dataloader workers). Bigger batch => more GPU RAM used "
                         "(activations scale with it). CRITICAL: BOTH the train AND validation "
                         "split must have >= batch_size GT segments, or the batched loader hangs "
                         "trying to fill a batch. Default 8 is safe with the current ~50 GT "
                         "segments (split 0.75 -> train=38, val=12). Rule of thumb: "
                         "batch_size <= round(N_gt * (1-split)).")
    ap.add_argument("--split", type=float, default=0.75,
                    help="train/val split fraction. With N GT segments, train gets round(N*split) "
                         "and val gets the rest -- each side must be >= batch_size. Default 0.75.")
    ap.add_argument("--reinit-head", action="store_true",
                    help="re-initialize the path-plan head from scratch (xavier) instead of "
                         "warm-starting from the teacher's pretrained weights. Default is warm "
                         "start: the head is 1.67M params (11.8%% of the model) and the dataset "
                         "is <1h, so relearning it from scratch is a major accuracy loss.")
    ap.add_argument("--grad-clip", type=float, default=None, metavar="NORM",
                    help="gradient clip norm passed to train.py (default: disabled, i.e. inf). "
                         "Worth setting (e.g. 1.0) when switching loss functions.")
    ap.add_argument("--mhp-loss", action="store_true",
                    help="train with the Laplacian MHP likelihood loss (numerically stable, "
                         "sigma-clamped) instead of the default KL-divergence distillation loss. "
                         "Passes --mhp_loss through to train.py.")
    ap.add_argument("--preview", action="store_true",
                    help="render the GT/prediction preview at the end of training and upload it to "
                         "wandb (not downloaded locally). Memory-safe -- capped to VIZ_FRAMES frames "
                         "(env, default 450) so it won't OOM the pod. Raise VIZ_FRAMES for a longer "
                         "preview if the pod has spare RAM.")
    ap.add_argument("--log-frequency", type=int, default=10, help="log train loss to wandb every N steps")
    ap.add_argument("--val-frequency", type=int, default=20,
                    help="run validation + log GT/prediction preview videos to wandb every N "
                         "steps. MUST be <= steps-per-epoch or it never triggers (tr_it resets "
                         "each epoch); this small dataset has ~27 steps/epoch, so keep it low.")
    ap.add_argument("--gen-gt", action="store_true", help="generate FULL ground truth before training")
    ap.add_argument("--gen-gt-count", type=int, default=0, metavar="N",
                    help="before training, generate ground truth for N MORE segments that don't "
                         "have it yet (grows the training set; skips already-done segments). "
                         "You have ~21 now, so --gen-gt-count 21 roughly doubles it to ~42.")
    ap.add_argument("--gt-only", action="store_true",
                    help="only grow ground truth (with --gen-gt-count/--gen-gt), then stop -- no "
                         "training or conversion. GT is saved on the volume for future runs.")
    ap.add_argument("--convert-only", action="store_true",
                    help="skip training; just convert the existing <date-it>.pth on the "
                         "volume to .onnx/.dlc and (with --wait) download them")
    ap.add_argument("--no-train", action="store_true", help="just provision; don't start training")
    ap.add_argument("--auto-stop", action="store_true", help="stop the pod when training finishes")
    ap.add_argument("--wait", action="store_true",
                    help="keep running until training finishes, download the model to "
                         "./trained_models/, then TERMINATE the pod")
    ap.add_argument("--snpe-sdk-url", default="",
                    help="direct download URL for the Qualcomm SNPE SDK zip "
                         "(enables .onnx -> .dlc conversion on the pod). "
                         "Can also be set via SNPE_SDK_URL env var.")
    ap.add_argument("--snpe-major", type=int, choices=(1, 2), default=2,
                    help="which SNPE converter generation to build the .dlc with. "
                         "2 = QAIRT/SNPE 2.x (default). 1 = SNPE 1.x, which is what "
                         "bukapilot's on-device runtime (namespace zdl) can actually "
                         "load -- requires --snpe-sdk-url pointing at a 1.x SDK zip.")
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

    # Shared boot loop from runpod_lib (create + wait-for-SSH + retry on stuck hosts).
    pod_id, host, port = boot_pod_with_retries(
        runpod, name=f"optrain-{args.date_it}", dc=args.dc, volume_id=args.volume,
        gpu_candidates=try_gpus, boot_timeout=args.boot_timeout, boot_retries=args.boot_retries)

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
