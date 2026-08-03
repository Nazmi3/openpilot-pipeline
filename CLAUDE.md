# Working notes for agents

## PURPOSE: this pipeline exists to train models for **bukapilot**

This repo is not an end in itself. It is the training pipeline for
**bukapilot** (KommuAssist, `kommu.ai`) — the sibling checkout at
`../bukapilot`. Everything trained here is ultimately meant to be deployed
on bukapilot hardware, so **the deployment target's model contract is what
matters**, not this repo's defaults.

bukapilot = openpilot `0.8.13` fork (`git describe` -> `v0.8.13-383-g...`),
release `9.8.2` at time of writing. Its model lives at
`../bukapilot/models/supercombo.onnx` (+ a compiled `supercombo.thneed`).

### Inputs MATCH — output layout DOES NOT

Verified by inspecting both ONNX graphs directly:

| | this repo's teacher | bukapilot |
|---|---|---|
| `input_imgs` | `[1,12,128,256]` | `[1,12,128,256]` ✅ |
| `desire` | `[1,8]` | `[1,8]` ✅ |
| `traffic_convention` | `[1,2]` | `[1,2]` ✅ |
| `initial_state` | `[1,512]` | `[1,512]` ✅ |
| **output** | **6472** | **11327** ❌ |
| params | 14,160,600 | 14,068,495 |

So **all input-side work transfers unchanged** — YUV I420 conversion, the
eon->medmodel warp, 2-frame 12-channel stacking, calibration rectification,
recurrent-state refeeding. That is the part that took the most debugging.

Output concat branches (same order in both = struct order in `driving.h`):

| # | meaning | this repo | bukapilot |
|---|---|---|---|
| 0 | plan | 4955 | **9905** |
| 1 | lanelines | 528 | 528 |
| 2 | laneline probs | 8 | **4** |
| 3 | road edges | 264 | 264 |
| 4 | leads | 102 | **55** |
| 5 | lead prob | 3 | 3 |
| 6 | desire state | 8 | 8 |
| 7 | meta | 48 | **4** |
| 8 | desire pred | 32 | 32 |
| 9 | pose | 12 | 12 |
| 10 | recurrent state | 512 | 512 |

### Retarget map (already worked out)

| | this repo | bukapilot |
|---|---|---|
| plan slice | `outputs[:, :4955]` | `outputs[:, :9905]` |
| recurrent slice | `outputs[:, 5960:]` | `outputs[:, 10815:]` — **use `[:, -512:]`, correct for both** |
| trainable plan head | `Gemm_959, Gemm_981, Gemm_983, Gemm_1036` | `Gemm_881, Gemm_903, Gemm_905, Gemm_958` |
| head params | 1,667,419 (11.8%) | 2,808,497 (~20%) |

bukapilot's 4 layers were verified **plan-exclusive** by forward reachability
(they reach output branch 0 and nothing else), same test used for this repo's.
Remember these are `onnx2pytorch` names = `{op_type}_{output_tensor_name}`,
e.g. bukapilot `Gemm_958` == raw ONNX node `Gemm_330`.

### OPEN QUESTION — resolve before spending GPU time

`../bukapilot/selfdrive/modeld/models/driving.h` does **not** match the shipped
`models/supercombo.onnx`:

* header implies plan `= 5 x (33*15*2 + 1) = 4955`; ONNX has **9905**
* header `ModelOutputLinesProb = 4 x 2 = 8` floats; ONNX has **4**

So the in-tree C++ parser describes a *different* model than the checked-in
ONNX (the header actually matches THIS repo's 6472 teacher). Before retargeting,
confirm which artifact bukapilot actually deploys — the `.onnx`, the `.thneed`
(compiled, possibly from another source), or a model fetched at runtime.
Retargeting to the wrong one silently produces a useless model.

### What retargeting still requires (not done yet)

1. Regenerate GT with bukapilot's supercombo as teacher (`gt_distill.h5` here
   was made with THIS repo's 6472 teacher — incompatible targets). Costs a
   full GT pass over the dataset.
2. Point `train.py`'s `pathplan_layer_names` at the bukapilot layer set above.
3. Replace hardcoded slices with the retarget map (or derive from the model).
4. Retrain + re-render comparison video.

Note `run1.pth` / `run2.pth` are for the **6472** teacher and cannot be loaded
into bukapilot at all.

---

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
