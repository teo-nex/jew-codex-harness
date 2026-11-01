@echo off
setlocal
cd /d "%~dp0"
call python -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 3)" >nul 2>&1
if not errorlevel 1 goto python
call python3 -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 3)" >nul 2>&1
if not errorlevel 1 goto python3
call py -3 -I -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 3)" >nul 2>&1
if not errorlevel 1 goto py
echo Python 3.11+ is required. Install it and make python or py available on PATH. 1>&2
exit /b 2
:python
call python -m harness.cli %*
exit /b %errorlevel%
:python3
call python3 -m harness.cli %*
exit /b %errorlevel%
:py
call py -3 -m harness.cli %*
exit /b %errorlevel%
