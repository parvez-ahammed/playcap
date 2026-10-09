@echo off
rem playcap portable launcher. Double-click to open the playcap UI.
rem
rem Program files live in "app\" next to this file and are replaced on upgrade.
rem Your settings, queue and recordings live in the data folder, which is never
rem inside "app\" and is not part of the download, so upgrading cannot touch it.
rem Data folder, first match wins:
rem   1. the PLAYCAP_DATA environment variable
rem   2. the path on the first line of data-location.txt next to this file;
rem      %VARIABLES% in it are expanded, e.g. %USERPROFILE%\playcap
rem      (the installer ships one; the portable zip does not)
rem   3. "data\" next to this file
setlocal
set "APP=%~dp0app"
set "DATA=%PLAYCAP_DATA%"
if not defined DATA if exist "%~dp0data-location.txt" set /p DATA=<"%~dp0data-location.txt"
rem Outside any ( ) block on purpose: "call" re-expands the text, turning a
rem literal %USERPROFILE% read from the file into the real folder.
if defined DATA call set "DATA=%DATA%"
if not defined DATA set "DATA=%~dp0data"
rem A trailing backslash would escape the closing quote of --root "...".
if "%DATA:~-1%"=="\" set "DATA=%DATA:~0,-1%"

if not exist "%DATA%" mkdir "%DATA%"
if not exist "%DATA%\examples\demo" xcopy /e /i /q /y "%APP%\examples\demo" "%DATA%\examples\demo" >nul

cd /d "%DATA%"
"%APP%\python\python.exe" -m playcap ui --root "%DATA%" %*
if errorlevel 1 pause
