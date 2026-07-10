@echo off
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

set NO_PROXY=localhost,127.0.0.1,0.0.0.0
set no_proxy=localhost,127.0.0.1,0.0.0.0

:: bat 模式用 9010/9011，docker 模式用 8010/8011，两者可同时运行
set AETHER_PROGRESS_PORT=9011

set "PROJECT_DIR=%~dp0"

echo ========================================
echo   Aether Demo Startup (BAT + Q to quit)
echo ========================================
echo Project directory: %PROJECT_DIR%

:: ---- Precise cleanup of old Aether backend processes ----
:: Logic moved to scripts\cleanup_aether.ps1 to avoid bat inline-PowerShell escaping issues.
echo Cleaning up existing Aether backend processes...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%scripts\cleanup_aether.ps1"

:: Fallback: kill any LISTENING process on port 9010 (in case a non-uvicorn process holds it)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":9010" ^| findstr "LISTENING" 2^>nul') do (
    echo Killing lingering process on port 9010 (PID: %%a)
    taskkill /PID %%a /T /F >nul 2>&1
)

:: Kill any LISTENING process on port 5173 (old frontend dev server) before starting a new one
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173" ^| findstr "LISTENING" 2^>nul') do (
    echo Killing lingering process on port 5173 (PID: %%a)
    taskkill /PID %%a /T /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo Starting Docker services (MQTT, HA)...
docker compose -f "%PROJECT_DIR%docker-compose.yml" up -d mqtt homeassistant
timeout /t 10 /nobreak >nul

for /f "delims=" %%i in ('where conda 2^>nul') do set "CONDA_PATH=%%i"
if not defined CONDA_PATH (
    if exist "D:\anaconda\condabin\conda.bat" (
        set "CONDA_PATH=D:\anaconda\condabin\conda.bat"
    ) else (
        echo ERROR: conda not found in PATH
        pause
        exit /b 1
    )
)
echo Using conda: %CONDA_PATH%

if exist "%PROJECT_DIR%logs\ha_simulator.log" del "%PROJECT_DIR%logs\ha_simulator.log"
if exist "%PROJECT_DIR%logs\backend.log" del "%PROJECT_DIR%logs\backend.log"

echo Starting HA device simulator (hidden)...
start /b "" cmd /c ""%CONDA_PATH%" run -n yolo python "%PROJECT_DIR%ha_config\ha_simulator.py" >> "%PROJECT_DIR%logs\ha_simulator.log" 2>&1"
timeout /t 3 /nobreak >nul

echo Starting aether backend (port 9010, hidden)...
start /b "" cmd /c "cd /d "%PROJECT_DIR%" && "%CONDA_PATH%" run -n yolo python -m uvicorn app.main:app --host 0.0.0.0 --port 9010 --app-dir "%PROJECT_DIR%" >> "%PROJECT_DIR%logs\backend.log" 2>&1"

timeout /t 3 /nobreak >nul

echo Starting frontend dev server (port 5173, hidden)...
cd /d "%PROJECT_DIR%frontend"
if exist "node_modules\.vite\deps" (
    echo Clearing vite deps cache...
    rmdir /s /q "node_modules\.vite\deps" >nul 2>&1
)
start /b "" cmd /c "cd /d "%PROJECT_DIR%frontend" && npm run dev >> "%PROJECT_DIR%logs\frontend.log" 2>&1"
cd /d "%PROJECT_DIR%"

timeout /t 5 /nobreak >nul
start "" "http://localhost:5173"
echo ========================================
echo   Demo started at http://localhost:5173
echo   (Backend API at http://localhost:9010)
echo ========================================
echo.
echo Log files (use these commands to view):
echo   MQTT messages: docker logs mosquitto --tail 20 -f
echo   HA Simulator:  Get-Content logs\ha_simulator.log -Wait -Tail 20
echo   Backend:       Get-Content logs\backend.log -Wait -Tail 20
echo.
echo Type 'q' and press Enter to stop all services...
echo.

:wait_loop
set /p input="Enter 'q' to quit: "
if /i "%input%"=="q" goto cleanup
goto wait_loop

:cleanup
echo.
echo ========================================
echo   Stopping services...
echo ========================================
echo.

echo Stopping Aether backend + simulator (precise, not all python)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%scripts\cleanup_aether.ps1" -All
echo   Aether backend + simulator cleanup done

echo.
echo Stopping Node.js processes...
taskkill /F /IM node.exe 2>nul
if %errorlevel% equ 0 (
    echo   [OK] Node.js processes stopped
) else (
    echo   [INFO] No Node.js processes found
)

echo.
echo Stopping Docker containers...
docker compose -f "%PROJECT_DIR%docker-compose.yml" down 2>nul
if %errorlevel% equ 0 (
    echo   [OK] Docker containers stopped
) else (
    echo   [INFO] Docker containers already stopped
)

echo.
echo ========================================
echo   All services stopped
echo ========================================
exit /b
