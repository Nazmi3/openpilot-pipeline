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

python launch_runpod_training.py --date-it run1 --epochs 15 --mhp-loss --preview --wait %*

echo.
echo ============================================================
echo  Launcher finished. The pod keeps provisioning/training on
echo  RunPod; watch progress on wandb or SSH in (see output above).
echo  With --auto-stop the pod stops itself when training ends.
echo ============================================================
pause
