@echo off
setlocal
cd /d "%~dp0"
set "TCL_LIBRARY=%~dp0python-embed\tcl\tcl8.6"
set "TK_LIBRARY=%~dp0python-embed\tcl\tk8.6"
"%~dp0python-embed\python.exe" "%~dp0cat_widget.py"
echo.
echo ---- exit code: %ERRORLEVEL% ----
pause