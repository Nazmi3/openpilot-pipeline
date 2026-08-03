# Working notes for agents

Operational guidance for this repo. Read before launching anything on RunPod.

## Long-running jobs: poll infrequently

Everything real here runs on a remote RunPod pod and takes tens of minutes.
**Do not stream per-step progress** — a `Monitor` doing `tail -f | grep` on
training output emits a notification per log line and burns enormous context
for no benefit.

Instead: launch with `run_in_background: true`, then poll on a **5+ minute**
interval with a loop that exits when the job finishes. One compact status line
per poll.

```bash
# poller pattern: one line per 5 min, exits on completion
while true; do
  ep=$(grep -c 'Epoch [0-9]* done' "$LOG" 2>/dev/null || echo 0)
  echo "epochs ${ep}/15 | $(grep -oE 'Running loss: [0-9.]+' "$LOG" | tail -1)"
  grep -qE 'PIPELINE FINISHED|Pod terminated|ERROR:|Gave up' "$LOG" && break
  sleep 300
done
```

The `--wait` launchers already download artifacts and terminate the pod on
their own, so the background task's completion notification is usually enough
on its own — extra polling is optional.

### Expected durations (don't poll faster than these)

| Job | Duration | Poll every |
|---|---|---|
| Pod boot + SSH | 30–120 s (retries on wedged hosts) | — |
| Training, 15 epochs, 50 segments | **~60 min** (~3.7 min/epoch, 52 steps) | 5 min |
| `.pth` → `.onnx` → `.dlc` conversion | ~5–10 min (SDK download 2.3 GB first time) | 5 min |
| Prediction video, 60 s clip | ~6 min (teacher+student, CPU) | 2–5 min |
| Prediction video, 180 s / 3 segments | ~15 min | 5 min |
| GT generation | slow; parallelism via `GT_PAR`, `GT_THREADS_EACH` | 10 min |

Local log lines from `--wait` are a *filtered* view (the launcher only forwards
the newest matching line when it changes). For full history read the pod's
`/workspace/train_<date_it>.log` over SSH.

## Costs money

Pods bill while running. Always confirm before launching training or a long
render. `--wait` terminates the pod when done; without it, terminate manually.
After any failure, check for orphans:

```bash
python -u -c "import runpod,os; runpod.api_key=os.environ['RUNPOD_API_KEY']; print(len(runpod.get_pods()))"
```

## Gotchas that cost real time here

- **Launchers overlay local files onto the pod.** The pod clones upstream
  `nikebless/openpilot-pipeline` pinned at `b613373`; `FILES_TO_SYNC` in both
  launchers copies local edits over it. Add new files there or your changes
  silently don't apply.
- **Layer names are `onnx2pytorch` names**, `{op_type}_{output_tensor_name}` —
  not raw ONNX node names. `Gemm_1036` == raw node `Gemm_328`. A mismatch
  silently freezes every parameter; `load_trainable_model` now raises instead.
- **GPU/torch compat.** The pinned cu113 torch supports up to sm_86. Ada
  (sm_89) mostly works for training; Blackwell (sm_120) fails at the first conv
  with `CUDNN_STATUS_NOT_INITIALIZED`. The video renderer forces CPU
  (`CUDA_VISIBLE_DEVICES=""`) to sidestep this entirely.
- **moosefs (`/workspace`) can stall hard.** Cold reads sometimes hang in
  D-state for minutes. Stage inputs to container-local disk (`/root/...`)
  before heavy IO, and always `python -u` — block-buffered stdout has hidden a
  stall for 5+ minutes more than once.
- **`trained_models/` is gitignored.** `run1.pth` is 108 MB, over GitHub's
  100 MB hard limit. Never un-ignore it.
- **comma2k19 has no `liveCalibration`** and the env has no `capnp`, so
  `gt_distill.parse_logs` always fails here. Calibration comes from
  `global_pose/` instead (see `_load_segment_rpy_from_comma2k19`).

## Dataset reality

50 segments with GT = **59,500 frames = 0.83 h** (split 0.75 → 38 train /
12 val). 2,035 segments are downloaded but only 50 have `gt_distill.h5`, and
they cluster in very few drives of one vehicle. Grow with
`--gen-gt-count N`; it skips segments that already have GT.

Constraint: `batch_size <= round(N_gt * (1 - split))`. At 50 segments and
split 0.75 that caps batch at 12; the batched loader **hangs** if a split has
fewer segments than `batch_size`.

## Training config that matters

- **Use the distillation loss** (the default). `--mhp-loss` is for
  sensor-based `gt_real.h5`; paired with `gt_distill.h5` it discards ~90% of
  the teacher (1 of 5 hypotheses, no std, self-referential prob target) and
  hits a double-exponentiation bug in `path_laplacian_nll_loss`. `train.py`
  warns about this combination.
- **Warm-start the head** (now the default). `--reinit-head` restores the old
  xavier-from-scratch behaviour; on <1 h of data that relearns 1.67M params
  (11.8% of the model) from nothing.
- Only the path-plan head is trained — verified via ONNX reachability, those
  4 layers feed *only* output slice `[0:4955]`. Lanelines/road-edges come from
  frozen teacher weights, which is why they look identical in comparison videos.
- `train.py` saves the **final** epoch as `<date_it>.pth`, not the best
  validation checkpoint. Per-validation checkpoints are in `nets/checkpoints/`.
