@echo off
REM Lanzador Windows. Editá el .env antes de correr.
cd /d %~dp0
uv run python start_bot.py
pause
