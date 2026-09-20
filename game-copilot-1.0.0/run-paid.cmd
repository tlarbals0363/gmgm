@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-paid.ps1"
if errorlevel 1 pause
