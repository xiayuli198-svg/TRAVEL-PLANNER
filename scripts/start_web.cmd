@echo off
rem One-click launcher for travel-planner web UI (keep ASCII content)
chcp 65001 >nul
title travel-planner launcher
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src"
set "PYTHONIOENCODING=utf-8"
python scripts\launch_web.py
if errorlevel 1 (
  echo.
  echo Failed to start. Make sure Python is installed and run:
  echo   pip install fastapi uvicorn cryptography
  pause
)
