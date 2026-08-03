#!/usr/bin/env python3
"""
Sensor-based path-plan ground-truth generation.

This is the second GT option next to the teacher-distillation path in
`gt_distill/generate_gt.py`. Instead of running the pretrained supercombo
model, this derives the path plan directly from the device's ego-motion
(`liveLocationKalman` in the rlog): the future trajectory of the car *is*
the ground-truth path.

Output is written per segment as `gt_real.h5` in the SAME layout the training
dataloader/loss already expect for the teacher GT, so it is a drop-in second
source:

    plans       : float32 (T, 5, 2, 33, 15)   # 5 hypotheses, [mean, std], 33 anchors, 15 channels
    plans_prob  : float32 (T, 5)              # one-hot on the single real hypothesis

Only ONE real hypothesis exists per frame. It is placed in hypothesis slot 0's
mean channel; `plans_prob` is one-hot on slot 0. The std channel and the other
four hypotheses are left as zeros. This matches how `plan_mhp_loss`
(train/train.py, used with `--mhp_loss`) consumes the GT: it takes
`argmax(plans_prob)` to pick the single GT path and uses only its *mean*
(the GT std is ignored). Train with `--mhp_loss` when using this GT.

Each frame's 15 channels are: position xyz, velocity xyz, acceleration xyz,
orientation (roll/pitch/yaw), orientation-rate (roll/pitch/yaw rate) — the same
ordering used by the openpilot model and by gt_real/get_paths.ipynb.

Usage (run on a box where openpilot `common`/`tools` and `laika` are importable):

    python gt_real/generate_gt.py --recordings_basedir /path/to/segments

Requires (NOT available on the Windows dev box):
  - openpilot `tools.lib.logreader.LogReader`
  - laika (`laika.lib.orientation`)
Install openpilot with its tools per https://github.com/commaai/openpilot/tree/master/tools
and ensure `laika`, openpilot `common` and `tools` are on PYTHONPATH.
"""

import os
import sys
import argparse
from pathlib import Path

import numpy as np
import h5py
from tqdm import tqdm

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # noqa
from utils import printf, dir_path, PATH_TO_CACHE  # noqa

# T_IDXS: the 33 future time anchors (seconds) the model predicts, taken from
# https://github.com/commaai/openpilot/blob/master/selfdrive/common/modeldata.h
T_IDXS = np.array([
    0.,          0.00976562,  0.0390625,   0.08789062,  0.15625,
    0.24414062,  0.3515625,   0.47851562,  0.625,       0.79101562,
    0.9765625,   1.18164062,  1.40625,     1.65039062,  1.9140625,
    2.19726562,  2.5,         2.82226562,  3.1640625,   3.52539062,
    3.90625,     4.30664062,  4.7265625,   5.16601562,  5.625,
    6.10351562,  6.6015625,   7.11914062,  7.65625,     8.21289062,
    8.7890625,   9.38476562, 10.,
])

N_ANCHORS = len(T_IDXS)   # 33
N_CHANNELS = 15
N_HYPOTHESES = 5


def read_poses(segment_dir, logfile='rlog.bz2'):
    """Read ego-motion from the log. Mirrors gt_real/get_paths.ipynb."""
    from tools.lib.logreader import LogReader
    import laika.lib.orientation as orient

    log_path = os.path.join(segment_dir, logfile)
    if not os.path.exists(log_path):
        alt = os.path.join(segment_dir, 'raw_log.bz2')
        if os.path.exists(alt):
            log_path = alt
        else:
            raise FileNotFoundError(f'No {logfile} or raw_log.bz2 in {segment_dir}')

    logs = LogReader(log_path)
    kalman_msgs = [m.liveLocationKalman for m in logs if m.which() == 'liveLocationKalman']

    poses = {
        'positions_ecef': np.array([m.positionECEF.value for m in kalman_msgs]),
        'velocities_calib': np.array([m.velocityCalibrated.value for m in kalman_msgs]),
        'accelerations_calib': np.array([m.accelerationCalibrated.value for m in kalman_msgs]),
        'orientations_calib': np.array([m.calibratedOrientationECEF.value for m in kalman_msgs]),
        'orientations_ecef': np.array([m.orientationECEF.value for m in kalman_msgs]),
        'angular_velocities_calib': np.array([m.angularVelocityCalibrated.value for m in kalman_msgs]),
        'times': np.array([m.unixTimestampMillis for m in kalman_msgs]),
    }

    status = {
        'positions': np.array([m.positionECEF.valid for m in kalman_msgs]),
        'velocities': np.array([m.velocityECEF.valid for m in kalman_msgs]),
        'orientations_calib': np.array([m.calibratedOrientationECEF.valid for m in kalman_msgs]),
        'angular_velocities_calib': np.array([m.angularVelocityCalibrated.valid for m in kalman_msgs]),
        'status': np.array([m.status for m in kalman_msgs]),
        'inputsOK': np.array([m.inputsOK for m in kalman_msgs]),
        'gpsOK': np.array([m.gpsOK for m in kalman_msgs]),
        'sensorsOK': np.array([m.sensorsOK for m in kalman_msgs]),
        'deviceStable': np.array([m.deviceStable for m in kalman_msgs]),
    }

    if len(poses['orientations_ecef']) > 0:
        poses['orientations_quat'] = orient.euler2quat(poses['orientations_ecef'])

    return poses, status


def is_valid_segment(status):
    """All sensor/GPS/kalman flags must be good for the whole segment."""
    if len(status['status']) == 0:
        printf('FAILED notEmpty.')
        return False

    checks = {
        'gpsOK': np.all(status['gpsOK'] == True),
        'sensorsOK': np.all(status['sensorsOK'] == True),
        'inputsOK': np.all(status['inputsOK'] == True),
        'deviceStable': np.all(status['deviceStable'] == True),
        'positionsValid': np.all(status['positions'] == True),
        'velocitiesValid': np.all(status['velocities'] == True),
        'orientations_calibValid': np.all(status['orientations_calib'] == True),
        'angular_velocities_calibValid': np.all(status['angular_velocities_calib'] == True),
        'allValid': np.all([str(s) == 'valid' for s in status['status']]),
    }
    all_good = all(checks.values())
    if not all_good:
        failed = [k for k, v in checks.items() if not v]
        printf('FAILED', ', '.join(failed) + '.')
    return all_good


def local_positions_per_frame(poses):
    """For every reference frame i, express every frame's ECEF position in i's
    local (device) frame. Returns (N, N, 3). Mirrors get_paths.ipynb."""
    import laika.lib.orientation as orient

    ecef_positions = poses['positions_ecef']
    quats = poses['orientations_quat']
    n = len(ecef_positions)

    out = np.zeros((n, n, 3), dtype=np.float32)
    for i in range(n):
        ecef_from_local = orient.rot_from_quat(quats[i])
        local_from_ecef = ecef_from_local.T
        out[i] = np.einsum('ij,kj->ki', local_from_ecef, ecef_positions - ecef_positions[i])
    return out


def create_plans_gt(poses):
    """Build the single real path plan for every frame that has a full 10s future.

    Returns plan_single of shape (T_valid, 33, 15), where T_valid <= N.
    The tail of the segment (~last 10s, ~200 frames @ 20Hz) has no full future
    horizon and is therefore dropped.

    NOTE: positions are correctly expressed in each reference frame's local
    device frame. Velocities/accelerations/orientation(-rates) are taken from
    the calibrated per-frame values at the future timestamps (as in
    get_paths.ipynb) rather than re-rotated into the reference frame. Positions
    dominate the path loss; refine the others here if you need frame-exact
    kinematics.
    """
    positions_device = local_positions_per_frame(poses)  # (N, N, 3)
    times = poses['times'] / 1000.0  # seconds
    n = len(times)

    plans = []
    for step in range(n):
        times_rel = times - times[step]

        # For each T_IDXS anchor, take the first future sample at/after it.
        future_steps = []
        t_idx = 0
        for idx in range(step, n):
            if t_idx >= N_ANCHORS:
                break
            if times_rel[idx] >= T_IDXS[t_idx]:
                future_steps.append(idx)
                t_idx += 1

        if len(future_steps) < N_ANCHORS:
            # not enough future horizon left -> stop; rest of the segment is unusable
            break

        future_steps = np.array(future_steps[:N_ANCHORS])

        positions = positions_device[step][future_steps]              # (33, 3) in step's frame
        velocities = poses['velocities_calib'][future_steps]          # (33, 3)
        accelerations = poses['accelerations_calib'][future_steps]    # (33, 3)
        orientations = poses['orientations_calib'][future_steps]      # (33, 3)
        orientation_rates = poses['angular_velocities_calib'][future_steps]  # (33, 3)

        plan = np.hstack([positions, velocities, accelerations,
                          orientations, orientation_rates])           # (33, 15)
        plans.append(plan)

    if not plans:
        return None

    return np.stack(plans).astype(np.float32)  # (T_valid, 33, 15)


def to_training_format(plan_single):
    """Reshape (T, 33, 15) single real path into the (T, 5, 2, 33, 15) + (T, 5)
    layout the dataloader/loss expect. Real hypothesis -> slot 0 mean; one-hot
    prob on slot 0. Std channel and other hypotheses stay zero (unused by
    plan_mhp_loss)."""
    t = plan_single.shape[0]

    plans = np.zeros((t, N_HYPOTHESES, 2, N_ANCHORS, N_CHANNELS), dtype=np.float32)
    plans[:, 0, 0, :, :] = plan_single  # hypothesis 0, mean channel

    plans_prob = np.zeros((t, N_HYPOTHESES), dtype=np.float32)
    plans_prob[:, 0] = 1.0  # argmax -> 0

    return plans, plans_prob


def generate_ground_truth(path_to_segment, logfile='rlog.bz2', force=False):
    out_path = os.path.join(path_to_segment, 'gt_real.h5')

    if os.path.exists(out_path) and not force:
        printf('Ground truth already exists at:', out_path)
        return

    try:
        poses, status = read_poses(path_to_segment, logfile=logfile)
    except Exception as e:
        printf('Failed to read logs for', path_to_segment, ':', e)
        return

    if not is_valid_segment(status):
        printf('Skipping invalid segment:', path_to_segment)
        return

    plan_single = create_plans_gt(poses)
    if plan_single is None:
        printf('No usable frames in segment:', path_to_segment)
        return

    plans, plans_prob = to_training_format(plan_single)

    try:
        if os.path.exists(out_path):
            os.remove(out_path)
        with h5py.File(out_path, 'w') as h5:
            h5.create_dataset('plans', data=plans)
            h5.create_dataset('plans_prob', data=plans_prob)
        printf(f'Saved {plans.shape[0]} plans to {out_path}')
    except Exception as e:
        printf(f"Couldn't save ground truths at {path_to_segment}:", e)


def find_segments(recordings_basedir, cache_path):
    if os.path.exists(cache_path):
        printf('Using cached segment directories...')
        with open(cache_path, 'r') as f:
            return [line.strip() for line in f if line.strip()]

    printf('Finding segment directories...')
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    segments = []
    with open(cache_path, 'w') as f:
        for d, _, files in os.walk(recordings_basedir):
            has_video = 'video.hevc' in files or 'fcamera.hevc' in files
            has_log = 'rlog.bz2' in files or 'raw_log.bz2' in files
            if has_video and has_log:
                segments.append(d)
                f.write(d + '\n')
    return segments


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Generate sensor/pose-based path-plan ground truths (gt_real.h5).')
    parser.add_argument('--recordings_basedir', type=dir_path, default='train/datasets',
                        help='base directory with recordings')
    parser.add_argument('--cache', default=str(Path(PATH_TO_CACHE) / 'segments_real.txt'),
                        help='cache file listing segment dirs')
    parser.add_argument('--logfile', default='rlog.bz2', help='log filename to read')
    parser.add_argument('--force_gt', dest='force_gt', action='store_true',
                        help='regenerate even if gt_real.h5 exists')
    parser.set_defaults(force_gt=False)
    args = parser.parse_args()

    segments = find_segments(args.recordings_basedir, args.cache)
    printf('Generating sensor-based ground truths for', len(segments), 'segments...')
    for seg in tqdm(segments):
        printf('segment:', seg)
        generate_ground_truth(seg, logfile=args.logfile, force=args.force_gt)
        printf()
