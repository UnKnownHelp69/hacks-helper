@echo off
REM Starts the bot from the project folder (used by Task Scheduler or NSSM).
cd /d "%~dp0\..\.."
"venv\Scripts\python.exe" main.py
