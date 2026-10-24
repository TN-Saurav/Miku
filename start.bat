@echo off
setlocal
cd /d "%~dp0"

set "MIKU_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%MIKU_PYTHON%" (
    echo Miku's project virtual environment is missing:
    echo %MIKU_PYTHON%
    echo Create it with: py -3.11 -m venv .venv
    echo Then install with: .venv\Scripts\python.exe -m pip install .
    goto startup_failed
)

"%MIKU_PYTHON%" "%~dp0main.py" %*
set "MIKU_EXIT_CODE=%ERRORLEVEL%"
if not "%MIKU_EXIT_CODE%"=="0" (
    echo Miku stopped with exit code %MIKU_EXIT_CODE%.
    goto startup_failed
)
exit /b 0

:startup_failed
pause
exit /b 2
