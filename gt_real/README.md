# Creating path ground truths using sensors

Sensor/pose-based path-plan ground truth — the second GT option alongside the
teacher-distillation path in [`gt_distill/`](../gt_distill). Instead of running
the pretrained supercombo model, the future trajectory of the car (from the
device's `liveLocationKalman` ego-motion) *is* the ground-truth path.

## Usage

Run on a box where openpilot `common`/`tools` and `laika` are importable (e.g.
the Linux/RunPod training box — these are **not** installed on the Windows dev box):

```bash
python generate_gt.py --recordings_basedir /path/to/segments
```

Writes `gt_real.h5` in-place for every segment that has both a video
(`fcamera.hevc`/`video.hevc`) and a log (`rlog.bz2`/`raw_log.bz2`). The file has
the same `plans` / `plans_prob` datasets as the teacher's `gt_distill.h5`, so it
is a drop-in second source for the training dataloader.

## Training on it

```bash
python train/train.py --mhp_loss --gt_file_name gt_real.h5 --min_segment_len 950 ...
```

- `--mhp_loss` selects `plan_mhp_loss` (single-hypothesis loss). It takes the one
  real GT path (`argmax(plans_prob)`) and uses only its *mean* — the GT std is
  ignored, so the sensor GT only needs the mean filled.
- `--gt_file_name gt_real.h5` points the dataloader at the sensor GT.
- `--min_segment_len ~950`: sensor GT is ~200 frames shorter than teacher GT
  because the last ~10 s of a segment has no future horizon (33 anchors span
  10 s @ 20 Hz), so the tail is dropped.

## Format

`plans` is `(T, 5, 2, 33, 15)` and `plans_prob` is `(T, 5)`. Only one real
hypothesis exists per frame: it is placed in hypothesis slot 0's mean channel
and `plans_prob` is one-hot on slot 0. The 15 channels per anchor are position
xyz, velocity xyz, acceleration xyz, orientation (r/p/y), orientation-rate.

## Known limitations / TODO

- Positions are correctly expressed in each reference frame's local device
  frame. Velocities/accelerations/orientation(-rates) are taken from the
  calibrated per-frame values at the future timestamps rather than re-rotated
  into the reference frame (carried over from `get_paths.ipynb`). Positions
  dominate the path loss; refine the kinematic channels if needed.
- Uses unprocessed live positioning (`liveLocationKalman` from `rlog.bz2`). This
  could be improved by processing GNSS positions with Laika and then applying a
  Kalman filter (e.g. `LocKalman` from `loc_kf.py`).

## Install

- Install Openpilot with all dependencies using [these instructions](https://github.com/commaai/openpilot/tree/master/tools).
  Ensure `laika` and Openpilot's `common` and `tools` are on the Python path.
