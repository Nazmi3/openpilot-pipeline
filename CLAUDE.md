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

### WORKING ASSUMPTION: bukapilot runs the SAME 6472 model as this repo

**No retargeting is being done.** The table above is kept only for the day the
assumption breaks.

Justification: `../bukapilot/selfdrive/modeld/models/driving.h` is the code that
*parses* the model at runtime, and it describes plan
`= 5 x (33*15*2 + 1) = 4955` and `ModelOutputLinesProb = 4 x 2 = 8` floats —
i.e. exactly THIS repo's 6472 teacher, giving
`NET_OUTPUT_SIZE = 5960 + 512 = 6472`. The 11327-wide
`../bukapilot/models/supercombo.onnx` does NOT match that parser, so it is
presumed stale/unused.

=> `run2.onnx` / `run2.dlc` are already the correct shape for bukapilot.

Revisit only if the deployed model misbehaves in a way that smells like a
layout mismatch (garbage paths, nonsense lane probabilities). To re-check:
compare `NET_OUTPUT_SIZE` implied by `driving.h` against the actual model's
output width.

### Deploying to bukapilot

bukapilot picks its runner at BUILD time in `selfdrive/modeld/models/driving.cc`
(`use_thneed = not GetOption('no_thneed')` in `selfdrive/modeld/SConscript`):

| build | loads | this pipeline produces it? |
|---|---|---|
| default (`USE_THNEED`) | `models/supercombo.thneed` | **not directly** — built ON DEVICE from the `.dlc`, see below |
| `--no-thneed`, ONNX runner | `models/supercombo.onnx` | yes (`run2.onnx`) |
| `--no-thneed`, SNPE runner | `models/supercombo.dlc` | yes (`run2.dlc`) |

**`.thneed` is compiled FROM the `.dlc`, not from the `.onnx`.** This is NOT the
tinygrad flow of newer openpilot. `selfdrive/modeld/SConscript` ends with:

```
compile ../../models/supercombo.dlc ../../models/supercombo.thneed --binary
```

where `thneed/compile.cc` does `SNPEModel(argv[1], ..., USE_GPU_RUNTIME)`, runs one
inference, and dumps the captured OpenCL kernels. So the real deploy chain is
**sequential**: `.pth -> .onnx -> .dlc -> .thneed`, and the `.dlc` must be loadable
by the device's SNPE runtime even for a thneed build.

### The device needs an SNPE **1.41** DLC — 2.x will not load

Verified on the device (`comma@172.20.10.2:8022`, comma two / Android):

- runtime version, via `zdl::SNPE::SNPEFactory::getLibraryVersion()`: **1.41.0.2173**
- comma's own shipped `dmonitoring_model_q.dlc` says `converter-version=1.41.0.2173`
- our `.dlc` built with the default QAIRT SDK says `converterVersion 2.48.0.260626`
  and running `thneed/compile` on it fails with:
  `error_code=310; error_message=Dlc read failure. Failed to read archive file`

So with the **runtime the device currently has**, a `.dlc` must come from an SNPE
converter <= 1.43 (comma's own shipped `supercombo.dlc` was 1.43.0.2307 and loads
fine, so 1.41 runtime accepts 1.43 containers). `--snpe-major 1` in
`launch_runpod_training.py` exists for that.

**Sourcing a 1.x SDK has failed so far**: Qualcomm's public S3
(`Qualcomm_AI_Runtime_Community`) serves 2.x only, a plain Qualcomm ID shows "No
releases found", QPM is empty, and Kommu no longer has theirs.

### CLOSED: upgrading the DEVICE to the 2.x runtime does NOT work

Tested on the device 2026-08-04, all 13 aarch64-android backend libs staged
(`libSNPE`, `libQnnCpu`, `libQnnGpu`, `libQairtCpu`, `libQairtGpu`, system +
BackendExtensions). The 2.48 `libSNPE.so` loads and reports its version, and it
opens our 2.48 `.dlc` fine -- but **every runtime fails to initialise**:

| runtime | error from `isRuntimeAvailable()` (all 3 check options) |
|---|---|
| CPU | `2006 QNN_COMMON_ERROR_PLATFORM_NOT_SUPPORTED: Attempt to use QNN API on an unsupported platform` |
| GPU / GPU_FLOAT16 | `5000 QNN_BACKEND_ERROR_CANNOT_INITIALIZE: Backend failed to initialize` |
| DSP | `1200 No backend library matched for this build and target` |

Forcing `SNPEBuilder::build()` anyway fails at the first layer:
`No backend could validate Op=Conv_0 Type=Conv2d`.

Note **CPU** fails with an explicit *platform not supported* -- so this is not an
Adreno 530 problem and not a missing-library problem, it is QNN 2.x refusing
MSM8996 outright. Do not retry this with a different 2.x version or more libs.

The one useful positive: the 2.48 headers alias into `zdl::`
(`ALIAS_IN_ZDL_NAMESPACE`) and bukapilot-style code compiles against them
unchanged -- irrelevant now, but recorded so nobody re-derives it.

=> The ONLY path to a deployable model is an SNPE converter <= 1.43.

### Historical detail: what upgrading WOULD have needed

Verified by reading the 2.48 SDK zip's central directory (range requests, no full
download needed):

- `lib/aarch64-android/libSNPE.so` exists; `.note.android.ident` says
  **minSdkVersion 21** and the device is **API 23** -> not an OS blocker.
- `lib/aarch64-android/libQairtGpu.so` links `libOpenCL_Adreno.so` / `libOpenCL.so`;
  the device has `/system/vendor/lib64/libOpenCL.so`.
- The 2.x C++ headers still alias into the old namespace --
  `ALIAS_IN_ZDL_NAMESPACE(SNPE, SNPEFactory)` -- so bukapilot's `zdl::` code is
  source-compatible.

All of which was true, and none of which mattered -- see the CLOSED section above.
The staging kit (probe source, deploy/rollback scripts, extracted libs+headers) is
at `~/snpe248_gate0` if it is ever needed for a different device.

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
