@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-free.ps1"
if errorlevel 1 pause
