"""
Generate a ~1 minute video of model predictions on a driving segment.

Self-contained test/utility that reproduces *exactly* the visualization done
during training (`train.visualize_predictions`):

  * frames are pre-processed with the same pipeline as training
    (bgr -> YUV I420 -> med-model transform via ``utils.transform_frames``),
  * the recurrent GRU state is refed frame-to-frame just like training,
  * predictions are rendered with the **same calibration source used during
    training** -- ``utils.TRAIN_VIZ_CALIB_RPY`` (zero roll/pitch/yaw), via the
    shared ``utils.draw_visualization`` helper.

Two modes:

  * single model (``--model``): one 640x480 frame per timestep.
  * teacher+student (``--model`` + ``--teacher-model``): 1280x480 side-by-side.
    The teacher (usually the untouched ``common/models/supercombo.onnx``) is
    drawn on the left, the student (the trained ``.pth``) on the right, so
    distillation-driven divergences are visually obvious.

Design note: unlike ``dataloader.load_transformed_video`` (which decodes all
frames into RAM before returning), this test **streams**: read N frames ->
preprocess -> infer -> write -> next chunk. That keeps memory bounded (the
network-volume decode of a 60 s clip otherwise sat in D-state waiting on
~2 GB of numpy allocations) and prints progress as it goes.

Run directly (writes an .mp4):

    python train/test_prediction_video.py \
        --model trained_models/run1.pth \
        --teacher-model common/models/supercombo.onnx \
        --segment /path/to/segment \
        --output trained_models/run1_teacher_vs_student_1min.mp4

Or configure via environment variables and run under pytest:

    PRED_VIDEO_MODEL=trained_models/run1.pth \
    PRED_VIDEO_SEGMENT=/path/to/segment \
    pytest train/test_prediction_video.py -s

If the model or segment cannot be found, the pytest test is skipped.
"""

import os
import sys
import argparse
import time

import numpy as np
import cv2

# make sure the repo root is importable whether run from repo root or train/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_REPO_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from utils import (  # noqa: E402
    extract_preds, draw_visualization, printf,
    bgr_to_yuv, transform_frames, create_image_canvas, FULL_FRAME_SIZE,
)
from model import load_inference_model  # noqa: E402


# defaults chosen to match training visualization
PLOT_IMG_WIDTH = 640
PLOT_IMG_HEIGHT = 480
FPS = 20                       # comma frames are logged at 20 Hz
DEFAULT_DURATION_S = 60        # "1 minute"
CHUNK_FRAMES = 60              # process ~3 s at a time (~90 MB YUV/chunk)
DEFAULT_MODEL = os.path.join(_REPO_ROOT, 'trained_models', 'run1.pth')
DEFAULT_TEACHER = os.path.join(_REPO_ROOT, 'common', 'models', 'supercombo.onnx')
DEFAULT_OUTPUT = os.path.join(_REPO_ROOT, 'trained_models', 'run1_prediction_1min.mp4')

# on-frame label style (matches wandb preview vibe: white text, dark shadow)
_LABEL_FONT = cv2.FONT_HERSHEY_SIMPLEX
_LABEL_SCALE = 0.9
_LABEL_THICK = 2


def _resolve_video_path(segment_path):
    """Match dataloader.load_transformed_video's file-name conventions."""
    for name in ('video.hevc', 'fcamera.hevc'):
        p = os.path.join(segment_path, name)
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f'No video.hevc/fcamera.hevc under {segment_path}')


def _parse_rpy_arg(s):
    """Parse an rpy CLI arg like "0.0 0.03 -0.01" or "0.0,0.03,-0.01"."""
    if s is None:
        return None
    parts = [p for p in s.replace(',', ' ').split() if p]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(f'--rpy-calib expects 3 floats, got: {s!r}')
    return [float(p) for p in parts]


def _load_segment_rpy_from_livecalibration(segment_path, openpilot_dir=None):
    """Read liveCalibration RPY from a segment's raw_log.bz2 / rlog.bz2 (real
    openpilot dashcam recordings) via cereal/capnp, and return the mean over
    non-zero frames -- matches what the training viz patch (`_set_viz_rpy`
    in the RunPod PROVISION_SCRIPT) does.

    Requires the `capnp` + `cereal` packages, which are NOT part of this
    repo's environment.yml -- so this normally fails with ImportError.
    Kept as the first-choice path (it's the ground-truth source when it
    *is* available); callers should fall back to
    `_load_segment_rpy_from_comma2k19` when this returns None.
    """
    if openpilot_dir is None:
        openpilot_dir = os.path.join(_REPO_ROOT, 'common')
    try:
        from gt_distill.parse_logs import parse_logs
    except Exception as e:
        printf(f'[info] gt_distill.parse_logs unavailable ({e}); '
               f'will try comma2k19 global_pose instead')
        return None
    try:
        rpy_seg, _ext = parse_logs(segment_path, openpilot_dir)
    except Exception as e:
        printf(f'[info] parse_logs failed ({e}); will try comma2k19 global_pose instead')
        return None
    if rpy_seg is None:
        return None
    rpy = np.asarray(rpy_seg).reshape(-1, 3)
    rpy = rpy[np.any(rpy != 0, axis=1)]
    if rpy.shape[0] == 0:
        return None
    return [float(v) for v in rpy.mean(axis=0)]


def _load_segment_rpy_from_comma2k19(segment_path, min_speed=3.0):
    """Compute a stable (roll, pitch, yaw) calibration estimate from a
    comma2k19-format segment's `global_pose/` directory. No capnp/cereal
    dependency -- just the numpy arrays comma2k19 ships, decoded with this
    repo's own `common.transformations` module.

    This is the camera *mounting* calibration -- `device_from_calib` -- the
    same quantity real openpilot's `liveCalibration.rpyCalib` reports, and
    exactly what `utils.Calibration`/`get_view_frame_from_calib_frame`
    consumes. We build it directly as a rotation matrix and read the angles
    back with `rot2euler`, so the sign/order convention is guaranteed to
    match `Calibration` (an earlier NED-euler attempt used a different
    Tait-Bryan order and produced a flipped pitch -> paths pitched into the
    road).

    Per frame:
      * device frame in ECEF comes from `frame_orientations`
        (`rot_from_quat` gives `ecef_from_device`, per camera.device_from_ecef).
      * the "calib" (road) frame in ECEF is x=forward (direction of travel
        from `frame_velocities`), z=down (toward earth centre, ~gravity),
        y=right; a right-handed x-fwd/y-right/z-down frame -- the same
        convention `Calibration` expects.
      * device_from_calib = ecef_from_device^T @ ecef_from_calib; its euler
        angles are the calibration rpy.
    We take the median over moving frames so bumps/turns don't skew it.

    Returns None if `global_pose/` isn't present (e.g. a real openpilot
    recording, not comma2k19) or too few valid (moving) samples exist.
    """
    gp_dir = os.path.join(segment_path, 'global_pose')
    try:
        orientations = np.load(os.path.join(gp_dir, 'frame_orientations'))  # (N,4) ecef_from_device
        positions = np.load(os.path.join(gp_dir, 'frame_positions'))        # (N,3) ecef
        velocities = np.load(os.path.join(gp_dir, 'frame_velocities'))      # (N,3) ecef
    except Exception as e:
        printf(f'[info] no comma2k19 global_pose under {segment_path} ({e})')
        return None

    from common.transformations.orientation import rot_from_quat, euler_from_rot

    speed = np.linalg.norm(velocities, axis=1)
    valid = speed > min_speed
    if valid.sum() < 10:
        printf(f'[warn] only {int(valid.sum())} samples above {min_speed} m/s; '
               f'not enough to estimate calibration from global_pose')
        return None

    R_ecef_from_device = rot_from_quat(orientations)            # (N, 3, 3)

    # calib frame axes expressed in ECEF (right-handed x-fwd, y-right, z-down)
    fwd = velocities / speed[:, None]                          # x: direction of travel
    down = -positions / np.linalg.norm(positions, axis=1, keepdims=True)  # z: ~toward earth centre
    right = np.cross(down, fwd)                                # y
    right /= np.linalg.norm(right, axis=1, keepdims=True)
    down = np.cross(fwd, right)                                # re-orthogonalize z
    R_ecef_from_calib = np.stack([fwd, right, down], axis=2)   # columns are the axes

    R_device_from_ecef = np.transpose(R_ecef_from_device, (0, 2, 1))
    R_device_from_calib = np.matmul(R_device_from_ecef, R_ecef_from_calib)

    rpy = euler_from_rot(R_device_from_calib)                  # (N, 3) matching Calibration's convention
    rpy = rpy[valid]
    return [float(x) for x in np.median(rpy, axis=0)]


def load_segment_rpy(segment_path, openpilot_dir=None):
    """Best-effort calibration RPY for a segment: try real liveCalibration
    logs first (ground truth when available), then comma2k19's global_pose
    (self-contained, no capnp needed). Returns None -- meaning "fall back
    to TRAIN_VIZ_CALIB_RPY (zero)" -- only if neither source is usable."""
    rpy = _load_segment_rpy_from_livecalibration(segment_path, openpilot_dir)
    if rpy is not None:
        return rpy
    return _load_segment_rpy_from_comma2k19(segment_path)


def _zoom_matrix():
    """Same CALIB_BB_TO_FULL as dataloader.load_transformed_video."""
    zoom = FULL_FRAME_SIZE[0] / PLOT_IMG_WIDTH
    return np.asarray([[zoom, 0., 0.],
                       [0., zoom, 0.],
                       [0., 0., 1.]])


def _annotate(rgb, label):
    """Stamp a label in the top-left with a soft shadow so it stays legible on
    both bright pavement and shadow. In-place on `rgb` (uint8 HxWx3)."""
    org = (12, 32)
    # shadow
    cv2.putText(rgb, label, (org[0] + 2, org[1] + 2), _LABEL_FONT, _LABEL_SCALE,
                (0, 0, 0), _LABEL_THICK + 1, cv2.LINE_AA)
    cv2.putText(rgb, label, org, _LABEL_FONT, _LABEL_SCALE,
                (255, 255, 255), _LABEL_THICK, cv2.LINE_AA)
    return rgb


def _run_one(run_model, stacked_frame, desire, traffic_convention, recurrent_state):
    """One forward pass for one frame; returns (preds, updated_recurrent_state)."""
    inputs = {
        'input_imgs': stacked_frame.astype(np.float32),
        'desire': desire,
        'traffic_convention': traffic_convention,
        'initial_state': recurrent_state,
    }
    outs, new_recurrent = run_model(inputs)
    lanelines, road_edges, best_path = extract_preds(outs)[0]
    return (lanelines, road_edges, best_path), new_recurrent


def _decode_stacked_frames(video_path, upto, rpy_calib=None):
    """Decode + preprocess the first `upto` frames of a video into model-ready
    12-channel stacked inputs, plus the plotting RGB canvases. Used by the
    calibration montage (which needs the model warmed up to a given frame).

    `rpy_calib` rectifies the model input into the calibrated frame, matching
    the on-car pipeline (see utils.transform_frames)."""
    zoom_matrix = _zoom_matrix()
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f'cv2.VideoCapture failed to open {video_path}')
    ok, prev_bgr = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError(f'empty video: {video_path}')

    yuv = [bgr_to_yuv(prev_bgr)]
    rgb = []
    for _ in range(upto):
        ok, bgr = cap.read()
        if not ok:
            break
        yuv.append(bgr_to_yuv(bgr))
        rgb.append(create_image_canvas(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB),
                                       zoom_matrix, PLOT_IMG_HEIGHT, PLOT_IMG_WIDTH))
    cap.release()

    prepared = transform_frames(np.asarray(yuv), rpy_calib=rpy_calib)
    n = len(rgb)
    stacked = np.zeros((n, 12, 128, 256), dtype=np.uint8)
    for i in range(n):
        stacked[i] = np.vstack(prepared[i:i + 2])[None].reshape(12, 128, 256)
    return stacked, rgb


def generate_calib_montage(model_path, segment_path, output_png, rpy_base=None,
                           frame_idx=200, openpilot_dir=None):
    """Render ONE frame's student prediction with a grid of candidate rpy
    calibrations, tiled into a labeled PNG. Fast way to pick the right camera
    calibration visually before committing to a full 60s render.

    `rpy_base` is the auto-estimated calibration (from `load_segment_rpy`);
    the grid sweeps pitch and yaw around it (plus zero and half-scale) so you
    can eyeball which lands the path on the road.
    """
    if rpy_base is None:
        rpy_base = load_segment_rpy(segment_path, openpilot_dir) or [0.0, 0.0, 0.0]
    r0, p0, y0 = rpy_base
    deg = np.pi / 180.0

    # candidate list: (label, [roll, pitch, yaw])
    candidates = [
        ('zero', [0.0, 0.0, 0.0]),
        ('auto (full)', [r0, p0, y0]),
        ('auto / 2', [r0, p0 / 2, y0 / 2]),
    ]
    # pitch sweep (yaw at auto), then yaw sweep (pitch at auto)
    for dp in (-1.0, 1.0, 2.0):
        candidates.append((f'pitch{dp:+.0f}deg', [r0, p0 + dp * deg, y0]))
    for dy in (-1.0, 1.0, 2.0):
        candidates.append((f'yaw{dy:+.0f}deg', [r0, p0, y0 + dy * deg]))

    printf(f'=> Montage: model up to frame {frame_idx}; base rpy(deg)='
           f'[{np.degrees(r0):.2f}, {np.degrees(p0):.2f}, {np.degrees(y0):.2f}]')
    video_path = _resolve_video_path(segment_path)
    _m, run_student = load_inference_model(model_path)

    # model input rectified with the base calibration (the grid below sweeps
    # only the DRAW projection, so input stays fixed across tiles)
    stacked, rgb = _decode_stacked_frames(video_path, frame_idx + 1, rpy_calib=rpy_base)
    n = len(rgb)
    if n == 0:
        raise RuntimeError('no frames decoded')
    fidx = min(frame_idx, n - 1)

    desire = np.zeros((1, 8), dtype=np.float32)
    traffic_convention = np.array([[0, 1]], dtype=np.float32)
    rs = np.zeros((1, 512), dtype=np.float32)
    preds = None
    for i in range(fidx + 1):
        preds, rs = _run_one(run_student, stacked[i:i + 1], desire, traffic_convention, rs)
    lanelines, road_edges, best_path = preds
    base_rgb = rgb[fidx]

    tiles = []
    for label, rpy in candidates:
        img = draw_visualization(lanelines, road_edges, best_path, base_rgb.copy(),
                                 rpy_calib=rpy,
                                 plot_img_width=PLOT_IMG_WIDTH, plot_img_height=PLOT_IMG_HEIGHT)
        _annotate(img, label)
        tiles.append(img)

    # tile into a 3-wide grid
    cols = 3
    while len(tiles) % cols != 0:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [np.concatenate(tiles[i:i + cols], axis=1) for i in range(0, len(tiles), cols)]
    montage = np.concatenate(rows, axis=0)

    os.makedirs(os.path.dirname(os.path.abspath(output_png)) or '.', exist_ok=True)
    cv2.imwrite(output_png, cv2.cvtColor(montage, cv2.COLOR_RGB2BGR))
    printf(f'=> Wrote calibration montage ({montage.shape[1]}x{montage.shape[0]}) to {output_png}')
    return output_png


def generate_prediction_video(model_path, segment_paths, output_path,
                              teacher_model_path=None, rpy_calib=None,
                              calibrate_input=True,
                              duration_s=DEFAULT_DURATION_S, fps=FPS,
                              chunk_frames=CHUNK_FRAMES):
    """Run the model(s) over one or more segments and write a prediction video.

    Streams frames in chunks so RAM stays bounded and progress prints as it
    goes. When ``teacher_model_path`` is given, produces a side-by-side
    (teacher | student) 1280x480 clip.

    ``rpy_calib`` is the camera's mounting roll/pitch/yaw. It is used in the
    two places a real car uses it:

      1. INPUT (``calibrate_input=True``): each frame is rectified into the
         calibrated frame before the model sees it -- what openpilot does
         on-car, so ``modeld`` always gets a canonically-mounted view. Without
         this the model sees the road tilted by the mount error, i.e. off its
         training distribution, degrading the predictions themselves.
      2. DRAW: predictions come out in the calibrated frame, so projecting
         them onto the *raw* display image needs the same rpy (drawing with
         ``[0,0,0]`` pitches the paths into the road). This mirrors openpilot's
         UI, which overlays on the raw camera feed.

    These are independent -- (1) affects prediction quality, (2) affects
    overlay alignment -- so applying both is not double-counting.

    ``rpy_calib=None`` (the normal case) resolves calibration PER SEGMENT --
    each segment is a different drive with its own camera mount, so a single
    fixed rpy would be wrong for all but one of them. Passing an explicit
    ``rpy_calib`` forces that value on every segment (useful for tuning).

    ``segment_paths`` may be a single path or a list; multiple segments are
    concatenated into one video, with the model's recurrent state reset at
    each boundary (see ``_stream_segment``).

    Returns (output_path, frames_written).
    """
    if isinstance(segment_paths, (str, bytes, os.PathLike)):
        segment_paths = [segment_paths]
    segment_paths = list(segment_paths)
    if not segment_paths:
        raise ValueError('no segments given')

    num_frames = int(round(duration_s * fps))
    zoom_matrix = _zoom_matrix()
    # fail fast if any segment lacks a video, before loading models/decoding
    for s in segment_paths:
        _resolve_video_path(s)

    # Load models FIRST so a missing weight file fails before we spend
    # minutes on video decode. Order matters: student is the primary model
    # whose output we care about, so load it first (its failure is louder).
    printf(f'=> Loading student model: {model_path}')
    t0 = time.time()
    _s_model, run_student = load_inference_model(model_path)
    printf(f'   student loaded in {time.time() - t0:.1f}s')

    run_teacher = None
    if teacher_model_path:
        printf(f'=> Loading teacher model: {teacher_model_path}')
        t0 = time.time()
        _t_model, run_teacher = load_inference_model(teacher_model_path)
        printf(f'   teacher loaded in {time.time() - t0:.1f}s')

    # Output dimensions: single = W x H, comparison = 2W x H (side-by-side).
    out_w = PLOT_IMG_WIDTH * (2 if run_teacher else 1)
    out_h = PLOT_IMG_HEIGHT
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or '.', exist_ok=True)
    writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*'mp4v'),
                             fps, (out_w, out_h))
    if not writer.isOpened():
        raise RuntimeError(f'Could not open VideoWriter for: {output_path}')

    mode = 'teacher | student' if run_teacher else 'student only'
    printf(f'=> {len(segment_paths)} segment(s), {num_frames} frames total '
           f'({num_frames / fps:.0f}s) @ {fps}fps, chunks of {chunk_frames} ({mode})')

    frames_written = 0
    t_stream = time.time()
    per_segment = []

    try:
        for seg_i, seg in enumerate(segment_paths):
            if frames_written >= num_frames:
                break
            remaining = num_frames - frames_written

            # Each segment is a DIFFERENT drive -> its own camera mount, so its
            # own calibration. Resolve per segment unless one was forced.
            seg_rpy = rpy_calib
            if seg_rpy is None:
                seg_rpy = load_segment_rpy(seg)
            if seg_rpy is None:
                printf(f'[warn] no calibration for {seg}; using zero rpy')
                seg_rpy = [0.0, 0.0, 0.0]

            seg_name = os.path.basename(seg.rstrip('/\\')) or seg
            printf(f'--- segment {seg_i + 1}/{len(segment_paths)}: {seg_name}')
            printf(f'    rpy(deg) = [{np.degrees(seg_rpy[0]):+.2f}, '
                   f'{np.degrees(seg_rpy[1]):+.2f}, {np.degrees(seg_rpy[2]):+.2f}]'
                   f'   input-calib: {"ON" if calibrate_input else "OFF"}')

            n = _stream_segment(seg, writer, run_student, run_teacher,
                                seg_rpy, calibrate_input, remaining, chunk_frames,
                                zoom_matrix, fps, frames_written, num_frames)
            frames_written += n
            per_segment.append((seg, n, seg_rpy))
    finally:
        writer.release()

    elapsed = time.time() - t_stream
    printf(f'=> Wrote {frames_written} frames ({frames_written / fps:.1f}s of video) '
           f'to {output_path} in {elapsed:.1f}s')
    for seg, n, r in per_segment:
        name = os.path.basename(seg.rstrip('/\\')) or seg
        printf(f'     {n:5d} frames ({n / fps:5.1f}s)  '
               f'rpy(deg)=[{np.degrees(r[0]):+.2f},{np.degrees(r[1]):+.2f},{np.degrees(r[2]):+.2f}]  '
               f'{name}')
    return output_path, frames_written


def _stream_segment(segment_path, writer, run_student, run_teacher,
                    rpy_calib, calibrate_input, max_frames, chunk_frames,
                    zoom_matrix, fps, frames_so_far, total_frames):
    """Stream one segment into an already-open writer. Returns frames written.

    The recurrent (GRU) state is created fresh here, so it RESETS at every
    segment boundary. Carrying it across a scene cut would feed the model
    temporal context from a different road/drive -- training resets the
    hidden state at segment boundaries for the same reason.
    """
    video_path = _resolve_video_path(segment_path)
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        printf(f'[warn] could not open {video_path}; skipping segment')
        return 0

    ok, prev_bgr = cap.read()
    if not ok:
        cap.release()
        printf(f'[warn] empty video {video_path}; skipping segment')
        return 0
    prev_yuv = bgr_to_yuv(prev_bgr)

    # fresh state per segment (see docstring)
    rs_student = np.zeros((1, 512), dtype=np.float32)
    rs_teacher = np.zeros((1, 512), dtype=np.float32)
    desire = np.zeros((1, 8), dtype=np.float32)
    traffic_convention = np.array([[0, 1]], dtype=np.float32)

    written = 0
    chunk_i = 0
    try:
        while written < max_frames:
            want = min(chunk_frames, max_frames - written)

            # 1) decode `want` new BGR frames + convert to YUV I420
            t_read = time.time()
            yuv_chunk = np.zeros((want + 1, FULL_FRAME_SIZE[1] * 3 // 2, FULL_FRAME_SIZE[0]),
                                 dtype=np.uint8)
            rgb_chunk = np.zeros((want, PLOT_IMG_HEIGHT, PLOT_IMG_WIDTH, 3), dtype=np.uint8)
            yuv_chunk[0] = prev_yuv
            got = 0
            for k in range(1, want + 1):
                ok, bgr = cap.read()
                if not ok:
                    break
                yuv_chunk[k] = bgr_to_yuv(bgr)
                rgb_chunk[k - 1] = create_image_canvas(
                    cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB), zoom_matrix,
                    PLOT_IMG_HEIGHT, PLOT_IMG_WIDTH)
                got += 1
            if got == 0:
                break
            prev_yuv = yuv_chunk[got].copy()

            # 2) med-model transform: 12-channel stacked pairs.
            #    rpy_calib rectifies into the calibrated frame first, exactly
            #    as openpilot does on-car before modeld (see transform_frames).
            t_pre = time.time()
            prepared = transform_frames(yuv_chunk[:got + 1],
                                        rpy_calib=rpy_calib if calibrate_input else None)
            stacked = np.zeros((got, 12, 128, 256), dtype=np.uint8)
            for i in range(got):
                stacked[i] = np.vstack(prepared[i:i + 2])[None].reshape(12, 128, 256)

            # 3) frame-by-frame inference (both models when in compare mode) + write
            t_inf = time.time()
            for i in range(got):
                # Student
                (s_ll, s_re, s_path), rs_student = _run_one(
                    run_student, stacked[i:i + 1], desire, traffic_convention, rs_student)
                student_rgb = draw_visualization(
                    s_ll, s_re, s_path, rgb_chunk[i].copy(),
                    rpy_calib=rpy_calib,
                    plot_img_width=PLOT_IMG_WIDTH, plot_img_height=PLOT_IMG_HEIGHT)

                if run_teacher is None:
                    frame_out = student_rgb
                else:
                    # Teacher
                    (t_ll, t_re, t_path), rs_teacher = _run_one(
                        run_teacher, stacked[i:i + 1], desire, traffic_convention, rs_teacher)
                    teacher_rgb = draw_visualization(
                        t_ll, t_re, t_path, rgb_chunk[i].copy(),
                        rpy_calib=rpy_calib,
                        plot_img_width=PLOT_IMG_WIDTH, plot_img_height=PLOT_IMG_HEIGHT)
                    _annotate(teacher_rgb, 'teacher (supercombo)')
                    _annotate(student_rgb, 'student (trained)')
                    # side-by-side: teacher | student
                    frame_out = np.concatenate([teacher_rgb, student_rgb], axis=1)

                # draw_* / annotate work in RGB; cv2.VideoWriter expects BGR
                writer.write(cv2.cvtColor(frame_out, cv2.COLOR_RGB2BGR))
                written += 1

            chunk_i += 1
            printf(f'    chunk {chunk_i}: +{got} -> {frames_so_far + written}/{total_frames}   '
                   f'(read {t_pre - t_read:.1f}s, pre {t_inf - t_pre:.1f}s, '
                   f'inf {time.time() - t_inf:.1f}s)')

            if got < want:
                printf('    reached end of segment')
                break
    finally:
        cap.release()
    return written


def _resolve_inputs():
    model_path = os.environ.get('PRED_VIDEO_MODEL', DEFAULT_MODEL)
    teacher_path = os.environ.get('PRED_VIDEO_TEACHER')  # None means "student only"
    segment_path = os.environ.get('PRED_VIDEO_SEGMENT')
    output_path = os.environ.get('PRED_VIDEO_OUTPUT', DEFAULT_OUTPUT)
    return model_path, teacher_path, segment_path, output_path


def test_generate_prediction_video():
    """Pytest entry point. Skips when model/segment aren't available."""
    import pytest

    model_path, teacher_path, segment_path, output_path = _resolve_inputs()

    if not os.path.exists(model_path):
        pytest.skip(f'Model not found: {model_path} (set PRED_VIDEO_MODEL)')
    if not segment_path or not os.path.isdir(segment_path):
        pytest.skip('Segment dir not set/found (set PRED_VIDEO_SEGMENT to a segment '
                    'directory containing video.hevc/fcamera.hevc)')

    # Teacher is optional in the pytest path -- if it's set and missing, skip
    # rather than surprising CI with a hard failure.
    if teacher_path and not os.path.exists(teacher_path):
        pytest.skip(f'Teacher not found: {teacher_path} (unset PRED_VIDEO_TEACHER '
                    'or point it at a valid .onnx)')

    out, n = generate_prediction_video(model_path, segment_path, output_path,
                                       teacher_model_path=teacher_path)

    assert os.path.exists(out), 'output video was not created'
    assert os.path.getsize(out) > 0, 'output video is empty'
    assert n >= FPS, f'expected at least {FPS} frames, got {n}'


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    env_model, env_teacher, env_segment, env_output = _resolve_inputs()
    parser.add_argument('--model', default=env_model,
                        help='path to trained student model (.pth or .onnx)')
    parser.add_argument('--teacher-model', default=env_teacher,
                        help='OPTIONAL path to teacher .onnx (e.g. common/models/'
                             'supercombo.onnx). When given, output is a side-by-side '
                             '(teacher | student) 1280x480 clip.')
    parser.add_argument('--segment', default=env_segment,
                        help='path to a segment directory (with video.hevc/fcamera.hevc)')
    parser.add_argument('--segments', nargs='+', default=None, metavar='DIR',
                        help='MULTIPLE segment dirs, concatenated into one video. Each '
                             'gets its own calibration (different drives = different '
                             'camera mounts) and a fresh recurrent state at the cut. '
                             'e.g. --segments /path/seg1 /path/seg2 /path/seg3')
    parser.add_argument('--output', default=env_output, help='output .mp4 path')
    parser.add_argument('--duration', type=float, default=DEFAULT_DURATION_S,
                        help='clip duration in seconds (default: 60)')
    parser.add_argument('--fps', type=int, default=FPS, help='frames per second (default: 20)')
    parser.add_argument('--chunk-frames', type=int, default=CHUNK_FRAMES,
                        help='frames to decode+infer per chunk (default: 60 == 3s)')
    parser.add_argument('--rpy-calib', type=_parse_rpy_arg, default=None,
                        help='roll pitch yaw (radians) used to project predictions '
                             'onto the raw image. e.g. "0 0.03 -0.01". If unset, '
                             'the segment\'s liveCalibration is auto-loaded via '
                             'gt_distill.parse_logs (matches training viz).')
    parser.add_argument('--segment-logs', default=None,
                        help='alt path to look up liveCalibration in (defaults to '
                             '--segment). Useful when the video was staged on local '
                             'disk but the log files still live on network storage.')
    parser.add_argument('--openpilot-dir', default=os.path.join(_REPO_ROOT, 'common'),
                        help="path to a checkout that provides tools.lib.logreader "
                             '(default: repo\'s common/)')
    parser.add_argument('--calib-montage', metavar='FRAME_IDX', type=int, default=None,
                        help='DEBUG: instead of a video, render frame FRAME_IDX with a '
                             'grid of candidate calibrations to a PNG (--output), to pick '
                             'the right rpy visually. e.g. --calib-montage 200')
    parser.add_argument('--no-calibrate-input', dest='calibrate_input',
                        action='store_false',
                        help='feed the model the RAW uncalibrated frame instead of '
                             'rectifying it into the calibrated frame first. Default is '
                             'to rectify, matching what openpilot does on-car before '
                             'modeld. Use this to A/B the effect on predictions.')
    parser.set_defaults(calibrate_input=True)
    args = parser.parse_args()

    if not os.path.exists(args.model):
        parser.error(f'model not found: {args.model}')
    if args.teacher_model and not os.path.exists(args.teacher_model):
        parser.error(f'teacher model not found: {args.teacher_model}')

    segments = args.segments if args.segments else ([args.segment] if args.segment else [])
    if not segments:
        parser.error('no segment given: pass --segment DIR or --segments DIR [DIR ...]')
    for s in segments:
        if not os.path.isdir(s):
            parser.error(f'segment directory not found: {s}')

    # rpy_calib: explicit --rpy-calib forces one value on every segment.
    # Left as None (the normal case) the renderer resolves calibration PER
    # segment, since each is a different drive with its own camera mount.
    rpy_calib = args.rpy_calib
    if rpy_calib is None and len(segments) == 1:
        # single-segment: resolve here so --segment-logs (pod stages the video
        # locally but logs stay on the network volume) is honoured
        log_seg = args.segment_logs or segments[0]
        rpy_calib = load_segment_rpy(log_seg, openpilot_dir=args.openpilot_dir)
        if rpy_calib is None:
            printf('[warn] no calibration found; using zero rpy '
                   '(paths will pitch off the road).')
        else:
            printf(f'=> segment rpy (roll, pitch, yaw) rad = '
                   f'[{rpy_calib[0]:+.5f}, {rpy_calib[1]:+.5f}, {rpy_calib[2]:+.5f}]  '
                   f'deg = [{np.degrees(rpy_calib[0]):+.2f}, '
                   f'{np.degrees(rpy_calib[1]):+.2f}, {np.degrees(rpy_calib[2]):+.2f}]')

    if args.calib_montage is not None:
        out_png = args.output
        if out_png.lower().endswith('.mp4'):
            out_png = out_png[:-4] + '_calib_montage.png'
        generate_calib_montage(args.model, segments[0], out_png,
                               rpy_base=rpy_calib, frame_idx=args.calib_montage,
                               openpilot_dir=args.openpilot_dir)
        return

    generate_prediction_video(args.model, segments, args.output,
                              teacher_model_path=args.teacher_model,
                              rpy_calib=rpy_calib,
                              calibrate_input=args.calibrate_input,
                              duration_s=args.duration, fps=args.fps,
                              chunk_frames=args.chunk_frames)


if __name__ == '__main__':
    main()
