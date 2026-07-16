@echo off
REM ============================================================
REM  Double-click to convert an ALREADY-TRAINED model on the
REM  volume (run1.pth) into .onnx + .dlc and download them to
REM  .\trained_models\  -- WITHOUT retraining (skips the ~1h).
REM
REM  It spins up a pod, runs only the conversion, downloads the
REM  files, then TERMINATES the pod. Requires RUNPOD_API_KEY.
REM
REM  To convert a different run, pass its name, e.g.:
REM      convert_to_dlc.bat --date-it myrun
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

python launch_runpod_training.py --date-it run1 --convert-only --wait %*

echo.
echo ============================================================
echo  Done. Look in .\trained_models\ for run1.onnx and run1.dlc
echo ============================================================
pause
