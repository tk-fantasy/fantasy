@echo off
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

set NO_PROXY=localhost,127.0.0.1,0.0.0.0
set no_proxy=localhost,127.0.0.1,0.0.0.0

set "PROJECT_DIR=%~dp0"

echo ========================================
echo   Aether App Startup (Production + PWA)
echo ========================================
echo Project directory: %PROJECT_DIR%

:: ---- Precise cleanup of old Aether backend processes ----
echo Cleaning up existing Aether backend processes...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%scripts\cleanup_aether.ps1"

:: Fallback: kill any LISTENING process on port 8010 (in case a non-uvicorn process holds it)
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8010" ^| findstr "LISTENING" 2^>nul') do (
    echo Killing lingering process on port 8010 (PID: %%a)
    taskkill /PID %%a /T /F >nul 2>&1
)
timeout /t 1 /nobreak >nul

echo Starting Docker services (MQTT, HA)...
:: 只起侧车容器（mqtt + homeassistant），不起 aether 容器：
:: 生产模式下后端由本机 conda uvicorn 运行（端口 8010），aether 容器会与本机抢端口。
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

:: ---- 构建前端（生产模式 + PWA）----
echo.
echo Building frontend (vite build + PWA)...
cd /d "%PROJECT_DIR%frontend"
call npm run build
if %errorlevel% neq 0 (
    echo ERROR: Frontend build failed. Check logs above.
    pause
    exit /b 1
)
echo Frontend build synced to app\static\frontend\
cd /d "%PROJECT_DIR%"

echo.
echo Starting HA device simulator (hidden)...
start /b "" cmd /c ""%CONDA_PATH%" run -n yolo python "%PROJECT_DIR%ha_config\ha_simulator.py" >> "%PROJECT_DIR%logs\ha_simulator.log" 2>&1"
timeout /t 3 /nobreak >nul

echo Starting aether backend (port 8010, hidden)...
:: 生产模式：前端由后端 spa_fallback 提供（无需 vite dev server）
start /b "" cmd /c "cd /d "%PROJECT_DIR%" && "%CONDA_PATH%" run -n yolo python -m uvicorn app.main:app --host 0.0.0.0 --port 8010 --app-dir "%PROJECT_DIR%" >> "%PROJECT_DIR%logs\backend.log" 2>&1"

echo Waiting for backend to be ready...
timeout /t 5 /nobreak >nul

:: 等待后端健康（最长 ~30s）：/api/auth/me 返回 401 即表示后端已就绪
set /a WAIT_COUNT=0
:wait_backend
set /a WAIT_COUNT+=1
curl -s -o nul -w "%%{http_code}" http://localhost:8010/api/auth/me 2>nul | findstr "401" >nul
if %errorlevel% equ 0 goto backend_ready
if %WAIT_COUNT% geq 15 goto backend_ready
timeout /t 2 /nobreak >nul
goto wait_backend

:backend_ready
echo Backend is ready.
start "" "http://localhost:8010"
echo ========================================
echo   App started at http://localhost:8010
echo   (PWA installable: click install icon in address bar)
echo   (Backend API at http://localhost:8010)
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
