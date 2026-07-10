@echo off
chcp 65001 >nul 2>&1
set "PROJECT_DIR=%~dp0"

echo ========================================
echo   Stopping Aether Demo Services
echo ========================================
echo.

echo Stopping Aether backend + simulator (precise, not all python)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%PROJECT_DIR%scripts\cleanup_aether.ps1" -All

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

if "%1"=="" pause
