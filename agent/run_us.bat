@echo off
REM 미국 주식 (실제 돈 · KIS) 진입점. 예: run_us.bat decide
REM SIGMA_MARKET=US → config\us · work_us · .env.us · 뉴욕 시간. 대회 계좌(키움)는 쓰지 않음
cd /d "%~dp0"
if not exist work_us mkdir work_us
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set SIGMA_MARKET=US
.venv\Scripts\python.exe run.py %* >> work_us\%1.log 2>&1
