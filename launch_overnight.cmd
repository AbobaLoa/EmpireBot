@echo off
cd /d C:\Users\Dima\Desktop\EmpireBot
.\.venv\Scripts\python.exe -u run.py --no-panel >> logs\overnight.stdout.log 2>> logs\overnight.stderr.log
