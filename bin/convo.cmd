@echo off
setlocal enabledelayedexpansion
rem convo plugin wrapper for native Windows (PowerShell / cmd.exe).
rem Requires the `convo` CLI installed via:
rem   pipx install tracine-convo   (or: uv tool install tracine-convo)
rem
rem Prefer a globally-installed `convo` binary on PATH (excluding self);
rem fall back to `python -m convo` if the package is importable.

set "self=%~f0"
for /f "delims=" %%i in ('where convo 2^>nul') do (
    set "candidate=%%~fi"
    if /i not "!candidate!"=="!self!" (
        "%%i" %*
        exit /b !errorlevel!
    )
)

python -c "import convo" >nul 2>&1
if %errorlevel% equ 0 (
    python -m convo %*
    exit /b %errorlevel%
)

echo convo: not installed. Run: pipx install tracine-convo (or: uv tool install tracine-convo) 1>&2
exit /b 1
