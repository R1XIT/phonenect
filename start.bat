@echo off
rem Start Phonenect in the system tray (no console window).
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m phonenect
