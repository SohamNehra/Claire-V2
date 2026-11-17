@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup Claire.ps1"
  if errorlevel 1 (
    pause
    exit /b 1
  )
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0claire2.ps1" app
if errorlevel 1 pause
