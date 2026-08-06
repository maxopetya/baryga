@echo off
setlocal
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: venv not found. Run setup first.
    exit /b 1
)

echo [%date% %time%] Collecting news...
".venv\Scripts\python.exe" -X utf8 -m finance.collect --hours 16 >> "output\collect.log" 2>&1
if errorlevel 1 (
    echo [%date% %time%] Collect failed. See output\collect.log
    exit /b 1
)

echo [%date% %time%] Exporting Markdown...
".venv\Scripts\python.exe" -X utf8 -m finance.export --hours 16
if errorlevel 1 (
    echo [%date% %time%] Export failed.
    exit /b 1
)

echo [%date% %time%] Done. Открой Claude Code и скажи "прогони скринер".
endlocal
