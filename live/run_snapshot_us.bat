@echo off
REM 미국 계좌 잔고 루프 (KIS · 실제 돈). 작업 스케줄러 SigmaUS-snapshot 이 부름
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set SIGMA_MARKET=US
if not exist ..\agent\work_us mkdir ..\agent\work_us
..\agent\.venv\Scripts\python.exe snapshot_us.py --loop >> ..\agent\work_us\snapshot.log 2>&1
