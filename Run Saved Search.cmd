@echo off
setlocal

set "PROJECT_ROOT=%~dp0"
set "VENV_DIR=%PROJECT_ROOT%.venv"
set "PYTHON=%VENV_DIR%\Scripts\python.exe"

cd /d "%PROJECT_ROOT%"

if not exist "%PYTHON%" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv "%VENV_DIR%"
    ) else (
        python -m venv "%VENV_DIR%"
    )
    if errorlevel 1 goto :error
)

"%PYTHON%" -c "import fastapi, jinja2, openpyxl, pydantic, playwright" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies...
    "%PYTHON%" -m pip install --disable-pip-version-check -r "%PROJECT_ROOT%requirements.txt"
    if errorlevel 1 goto :error
)

if not exist "%PROJECT_ROOT%config\search_preset.json" (
    echo Copy config/search_preset.example.json to config/search_preset.json and configure your search.
    pause
    exit /b 1
)

"%PYTHON%" -m playwright install chromium
if errorlevel 1 goto :error

"%PYTHON%" -m app.services.preset_runner --config "%PROJECT_ROOT%config\search_preset.json"
if errorlevel 1 goto :error

exit /b 0

:error
echo.
echo Saved search could not be completed.
pause
exit /b 1
