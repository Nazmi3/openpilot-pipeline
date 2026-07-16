@echo off
REM ============================================================
REM  Double-click (or run) this to create a RunPod pod, generate
REM  a 1-minute prediction video with the already-trained model
REM  on the volume, download it, and TERMINATE the pod.
REM
REM  Requires RUNPOD_API_KEY (set once with setx).
REM  Assumes the volume was already provisioned by a training run.
REM
REM  Pass extra flags after the file name, e.g.:
REM      run_test_video.bat --model run1 --duration 60
REM ============================================================
cd /d "%~dp0"

if "%RUNPOD_API_KEY%"=="" (
  echo.
  echo   RUNPOD_API_KEY is not set. Set it ONCE with:
  echo.
  echo       setx RUNPOD_API_KEY "your_runpod_key"
  echo.
  echo   then open a NEW terminal / re-run this file.
  echo.
  pause
  exit /b 1
)

python launch_runpod_test_video.py --model run1 --duration 60 %*

echo.
echo ============================================================
echo  Done. Look in .\trained_models\ for the mp4 (e.g.
echo  prediction_run1_60s.mp4). Pod is terminated (unless
echo  --keep-pod was passed).
echo ============================================================
pause
