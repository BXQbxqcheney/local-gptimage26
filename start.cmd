@echo off
REM img26 one-click launcher (Windows)
REM Checks Python + dependencies, installs if missing, then starts the server.

setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.10+ from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/2] Checking dependencies...
python -c "import fastapi, uvicorn, httpx, PIL, yaml, vtracer, anyio" >nul 2>nul
if errorlevel 1 (
    echo        Missing dependencies. Installing from requirements.txt ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your network or pip mirror.
        pause
        exit /b 1
    )
)
echo        OK.

echo [2/2] Starting server at http://127.0.0.1:8266 ...
echo        Press Ctrl+C to stop. Web console: http://127.0.0.1:8266
echo        First run: click the gear icon to configure your API providers.
REM Open the browser a few seconds later (after the server is up)
start "" cmd /c "timeout /t 4 /nobreak >nul & start "" http://127.0.0.1:8266"
python run.py %*

endlocal
pause
