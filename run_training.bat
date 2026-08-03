@echo off
REM ============================================================
REM  Double-click (or run) this to create a RunPod pod, provision
REM  everything on the network volume, and start training.
REM  Requires RUNPOD_API_KEY to be set once (see message below).
REM  Pass extra flags after the file name, e.g.:
REM      run_training.bat --gen-gt
REM ============================================================
cd /d "%~dp0"

if "%RUNPOD_API_KEY%"=="" (
  echo.
  echo   RUNPOD_API_KEY is not set. Set it ONCE with:
  echo.
  echo       setx RUNPOD_API_KEY "your_runpod_key"
  echo.
  echo   then open a NEW terminal / re-run this file.
  echo   ^(optional, only for a fresh volume^): setx WANDB_API_KEY "your_wandb_key"
  echo.
  pause
  exit /b 1
)

REM  Loss: KL-divergence distillation (train.py default) -- uses ALL of the
REM  teacher's output (5 hypotheses x mean+std + the probability distribution).
REM  Do NOT pass --mhp-loss here: that is for sensor-based gt_real.h5 and, with
REM  gt_distill.h5, discards ~90% of the teacher signal.
REM  Head is warm-started from the teacher's weights (see --reinit-head to opt out).
REM  --preview is off: the in-training wandb preview renders with zero rpy and
REM  is the step that used to OOM the pod. Use run_test_video.bat afterwards,
REM  which renders teacher-vs-student with correct per-segment calibration.
python launch_runpod_training.py --date-it run2 --epochs 15 --grad-clip 1.0 --wait %*

echo.
echo ============================================================
echo  Launcher finished. The pod keeps provisioning/training on
echo  RunPod; watch progress on wandb or SSH in (see output above).
echo  With --auto-stop the pod stops itself when training ends.
echo ============================================================
pause
