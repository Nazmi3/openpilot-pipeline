# Model inputs and where the data comes from

What the supercombo model is fed, how each tensor is built, and which file on
disk it ultimately comes from. Everything here is derived from the code in this
repo — each claim links to the line that implements it.

Model outputs are documented separately; see [CLAUDE.md](../CLAUDE.md) for the
output branch layout and the bukapilot deployment contract.

---

## At a glance

The model takes **four** inputs. Only the first carries image data; the other
three are small side-channel tensors fed in after the convolutional extractor.

| tensor | shape | dtype | where its value comes from |
|---|---|---|---|
| `input_imgs` | `(N, 12, 128, 256)` | `uint8` → float32 | two consecutive camera frames from `video.hevc` |
| `desire` | `(N, 8)` | float32 | **constant zeros** — not driven by data |
| `traffic_convention` | `(N, 2)` | float32 | **constant `[0, 1]`** — hard-coded, not read from the dataset |
| `initial_state` | `(N, 512)` | float32 | the model's own previous output, refed |

`N` is the batch dimension: `batch_size` during training, `1` during inference
and video rendering.

---

## 1. `input_imgs` — `(N, 12, 128, 256)`

### Source

`video.hevc` (or `fcamera.hevc`) inside a comma2k19 segment directory — the
road-facing camera recording. The filename fallback is at
[dataloader.py:32](../train/dataloader.py#L32).

### How it is built

The raw frame is 1164×874 ([utils.py:15](../utils.py#L15)) from the EON's camera,
focal length 910 px ([utils.py:17](../utils.py#L17)). Four steps turn a pair of
those into the input tensor:

1. **Decode + colour convert.** `cv2` reads BGR; `bgr_to_yuv`
   ([utils.py:304](../utils.py#L304)) converts to **YUV I420**, giving a
   `(1311, 1164)` planar buffer (`874 * 3/2` rows).

2. **Reproject to the model's camera.** `transform_img`
   ([utils.py:196](../utils.py#L196)) warps from `eon_intrinsics` to
   `medmodel_intrinsics` ([common/transformations/model.py:39](../common/transformations/model.py#L39)),
   output size 512×256, producing a `(384, 512)` YUV image. This is the
   "eon → medmodel" warp.

3. **Split the YUV planes into 6 channels.** `reshape_yuv`
   ([utils.py:266](../utils.py#L266)) turns each `(384, 512)` frame into
   `(6, 128, 256)`:

   | channel | content |
   |---|---|
   | 0 | Y, even rows, even cols |
   | 1 | Y, odd rows, even cols |
   | 2 | Y, even rows, odd cols |
   | 3 | Y, odd rows, odd cols |
   | 4 | U plane (half resolution) |
   | 5 | V plane (half resolution) |

   The four Y channels are a 2×2 de-interleave, not a downscale — no luma
   detail is lost.

4. **Stack two consecutive frames.** Channels `0–5` are frame `t-1`, channels
   `6–11` are frame `t`, giving `(12, 128, 256)`
   ([dataloader.py:94](../train/dataloader.py#L94)). Temporal context comes from
   this pairing plus the GRU state.

### Calibration rectification (optional but important)

`transform_frames` ([utils.py:322](../utils.py#L322)) takes an optional
`rpy_calib`. When given, the frame is first warped into the **calibrated
frame** using openpilot's own `pretransform_from_calib`, which is what
`calibrationd` does on-car before `modeld` ever sees the image.

- **Training and GT generation pass `None`** — the model sees the raw mounted
  view, tilted by whatever the camera's mount error is (~2° in this dataset).
- **The prediction-video renderer passes the real per-segment value**, so the
  model gets a canonically-mounted view, matching on-car behaviour.

This asymmetry is deliberate (leaving it `None` keeps the training path
byte-identical to upstream) but it does mean training data is slightly off the
distribution the model sees on a real car.

Because comma2k19 has no `liveCalibration` and this environment has no `capnp`,
the calibration is instead derived from `global_pose/` — see
`_load_segment_rpy_from_comma2k19` in
[test_prediction_video.py](../train/test_prediction_video.py).

---

## 2. `desire` — `(N, 8)`

**Always zeros.** Set at [train.py:177](../train/train.py#L177) and never
populated from the dataset.

`desire` is openpilot's lane-change / turn intent signal. Nothing in this
pipeline reads a driver's intent, so the model is trained and evaluated purely
in the "no desire" state. Any behaviour the real model has for commanded lane
changes is inherited unchanged from the frozen teacher weights.

---

## 3. `traffic_convention` — `(N, 2)`

**Hard-coded to `[0, 1]`** — zeros with index 1 set
([train.py:177-179](../train/train.py#L177)). It is not derived from the
dataset.

Index 1 is the flag bukapilot sets when `IsRHD` is true
(`selfdrive/modeld/models/driving.cc`: `const int idx = Params().getBool("IsRHD") ? 1 : 0;`),
i.e. right-hand-drive cars / left-hand traffic.

> **Worth checking.** This matches a Malaysian bukapilot deployment (left-hand
> traffic), but **not the training footage**: comma2k19 was recorded in
> California, which is left-hand-drive / right-hand traffic and would set index
> 0. So during training the flag contradicts the imagery.
>
> In practice the impact is likely small — it is a single constant across every
> training sample, so it acts as a fixed bias rather than a varying signal, and
> only the path-plan head is trained. But if lane-change or lane-position
> behaviour ever looks wrong on the device, this is a real thing to re-examine.
> Note the upstream [README](../README.md) describes this value as "RHT (Right
> Hand Traffic)", which reads as the opposite of what the bukapilot code does
> with index 1.

---

## 4. `initial_state` — `(N, 512)`

The GRU's recurrent state. **The model's own previous output, fed back in.**

- Read from the **last 512 values** of the output vector: `outs[:, -512:]`
  ([model.py:96](../train/model.py#L96) and
  [model.py:114](../train/model.py#L114)). The `-512:` slice is deliberate — it
  is correct for both this repo's 6472-wide model and bukapilot's 11327-wide
  one, unlike a hard-coded absolute offset.
- **Initialised to zeros** at the start of each segment, and **reset at every
  segment boundary**. Carrying it across a cut would feed the model temporal
  context from a different road entirely.
- During training the state is preserved between batches within a segment, and
  the first batch of each segment is not backpropagated (recurrent warm-up).

---

## Data sources on disk

### comma2k19 segment layout

One segment is one minute of driving. Verified directly against the dataset on
the RunPod volume:

```
<Chunk_N>/<dongle_id>|<timestamp>/<segment_number>/
├── video.hevc                      # road camera  -> input_imgs
├── global_pose/
│   ├── frame_times                 # per-frame timestamps (the master time base)
│   ├── frame_positions             # ECEF position   -> calibration, real-GT path
│   ├── frame_orientations          # ECEF quaternion -> calibration, real-GT path
│   └── frame_velocities            # ECEF velocity   -> calibration
├── processed_log/
│   ├── CAN/
│   │   ├── raw_can/                # t, address, src, data  -> driver-steering badge
│   │   ├── steering_angle/         # t, value (degrees)
│   │   ├── speed/
│   │   ├── wheel_speed/
│   │   └── radar/
│   ├── GNSS/
│   └── IMU/
├── gt_distill.h5                   # GENERATED by this repo (teacher outputs)
└── gt_real.h5                      # GENERATED by this repo (sensor-derived)
```

Note there is **no** `steering_torque` channel and **no** `liveCalibration` —
both are common assumptions that do not hold for comma2k19.

### What actually reads what

| consumer | reads | purpose |
|---|---|---|
| model input | `video.hevc` | the only true model input |
| calibration | `global_pose/{orientations,positions,velocities}` | camera mount rpy |
| real-GT path | `global_pose/{positions,orientations,frame_times}` | trajectory actually driven |
| driver-steering badge | `processed_log/CAN/raw_can`, `steering_angle` | who was steering |
| training target | `gt_distill.h5` or `gt_real.h5` | loss targets, **not** inputs |

### Ground-truth files (targets, not inputs)

Generated per segment by this repo, so they do not ship with comma2k19:

- **`gt_distill.h5`** — the frozen teacher model's own outputs, run over the
  segment: `plans`, `plans_prob`, `lanelines`, `laneline_probs`, `road_edges`,
  `road_edge_stds` ([gt_distill/generate_gt.py](../gt_distill/generate_gt.py)).
  This is the default and what the distillation loss expects.
- **`gt_real.h5`** — the path actually driven, derived from ego-motion rather
  than from a model: `plans` `(T, 5, 2, 33, 15)`, `plans_prob` `(T, 5)`
  ([gt_real/generate_gt.py](../gt_real/generate_gt.py)). Only one of the five
  hypotheses is real and the std channel is left zero, so it pairs with
  `--mhp-loss`, not the distillation loss.

See [gt_distill/README.md](../gt_distill/README.md) and
[gt_real/README.md](../gt_real/README.md).

---

## Practical constraints

- Segments are discovered by globbing for the GT filename
  ([dataloader.py:307](../train/dataloader.py#L307)), so **a segment without GT
  is invisible to training** even if its video is present.
- `MIN_SEGMENT_LENGTH = 1190` frames ([dataloader.py:27](../train/dataloader.py#L27)).
  A full minute at 20 Hz is ~1200 frames; `gt_real.h5` is shorter than
  `gt_distill.h5` because the last ~200 frames have no full 10 s future horizon.
- Frame rate is 20 Hz, so a 33-anchor 10 s prediction horizon spans ~200 frames.
- See [CLAUDE.md](../CLAUDE.md) for how much of the dataset actually has GT and
  the resulting batch-size limit.
