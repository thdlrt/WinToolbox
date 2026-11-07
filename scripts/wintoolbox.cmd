@echo off
setlocal
set "PYTHONPATH=%~dp0core"
"%~dp0python\python.exe" -m toolbox.cli --data-dir "%~dp0data" %*
exit /b %ERRORLEVEL%
