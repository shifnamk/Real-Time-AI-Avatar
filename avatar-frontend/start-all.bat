@echo off
echo ============================================================
echo STARTING AVATAR SYSTEM
echo ============================================================
echo.

cd /d "%~dp0"

echo [1/2] Starting Avatar Backend (Port 5000)...
start "Avatar Backend" cmd /k "node avatar-backend.cjs"
timeout /t 2 /nobreak >nul

echo [2/2] Starting Frontend (Port 5173)...
start "Frontend Dev Server" cmd /k "npm run dev"

echo.
echo ============================================================
echo ✅ SYSTEM STARTED
echo    - Avatar Backend: http://localhost:5000
echo    - Frontend: http://localhost:5173
echo ============================================================
echo.
echo Press any key to exit (servers will keep running)...
pause >nul

