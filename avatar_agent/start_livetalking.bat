@echo off
REM Start LiveTalking realtime server with LivePortrait integration
setlocal EnableDelayedExpansion

REM Set paths relative to this script
set "REPO_ROOT=%~dp0.."
set "PYTHON_EXE=%REPO_ROOT%\.venv\Scripts\python.exe"

echo Starting LiveTalking MuseTalk realtime server...
echo Repository: %REPO_ROOT%
echo.

REM Change to livetalking directory
cd /d "%REPO_ROOT%\livetalking"

REM Start LiveTalking with calm_avatar_realtime
REM To enable LivePortrait: Remove REM from the last 2 lines below
"%PYTHON_EXE%" app.py ^
    --transport webrtc ^
    --model musetalk ^
    --avatar_id calm_avatar_realtime ^
    --listenport 8010 ^
    --batch_size 16 ^
    --W 1280 ^
    --H 720 ^
    --max_session 5

pause

