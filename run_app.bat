@echo off
chcp 65001 >nul 2>&1
set "PROJECT_DIR=%~dp0"

echo ========================================
echo   Aether App (Docker)
echo ========================================

if not exist "%PROJECT_DIR%logs" mkdir "%PROJECT_DIR%logs"
set "BAT_LOG=%PROJECT_DIR%logs\run_app.log"
echo [%date% %time%] started > "%BAT_LOG%"

echo Checking Docker...
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Docker Desktop is not running.
    echo Please start Docker Desktop first.
    pause
    exit /b 1
)

echo Building and starting all services...
docker compose -f "%PROJECT_DIR%docker-compose.yml" up -d --build >> "%BAT_LOG%" 2>&1
if %errorlevel% neq 0 (
    echo ERROR: docker compose failed. Check logs\run_app.log
    pause
    exit /b 1
)

echo Waiting for backend to be ready...
set /a WAIT_COUNT=0
:wait_backend
set /a WAIT_COUNT+=1
curl -s -o nul -w "%%{http_code}" http://localhost:8010/api/auth/me 2>nul | findstr "401" >nul
if %errorlevel% equ 0 goto backend_ready
if %WAIT_COUNT% geq 30 goto backend_ready
timeout /t 2 /nobreak >nul
goto wait_backend

:backend_ready
echo [%date% %time%] Backend ready >> "%BAT_LOG%"
echo Starting app window...
start msedge --app=http://localhost:8010 --start-maximized
echo ========================================
echo   App is running.
echo   Close this window to keep services running.
echo   Run 'docker compose down' to stop.
echo ========================================
pause
