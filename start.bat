@echo off
setlocal
cd /d "%~dp0"
set "TCL_LIBRARY=%~dp0python-embed\tcl\tcl8.6"
set "TK_LIBRARY=%~dp0python-embed\tcl\tk8.6"
start "" "%~dp0python-embed\pythonw.exe" "%~dp0cat_widget.py"
exit /b 0