@echo off
rem Double-click to open the playcap UI for the folder this file is in.
cd /d "%~dp0"
python -m playcap ui %*
if errorlevel 1 pause
