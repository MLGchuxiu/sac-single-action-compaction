@echo off
rem ==========================================================================
rem update-sac.cmd - SAC plugin for DeepSeek Harness: install or update (Windows)
rem
rem   double-click this file        install / update the "desktop" profile
rem   update-sac.cmd -check         report only, change nothing
rem   update-sac.cmd -rollback      undo the last update
rem   update-sac.cmd -help          all options
rem
rem This launcher only finds a usable node.exe (on PATH, or the copy shipped
rem with DeepSeek Harness) and runs update-sac.js from the same folder.
rem Keep update-sac.cmd and update-sac.js together.
rem ==========================================================================
setlocal EnableExtensions
set "SAC_PAUSE=1"
set "NODE_EXE="

rem --- 1) node.exe on PATH ---
for /f "delims=" %%N in ('where node 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%N"

rem --- 2) node.exe bundled with DeepSeek Harness ---
if not defined NODE_EXE call :probe "%LOCALAPPDATA%\Programs"
if not defined NODE_EXE call :probe "%ProgramFiles%"
if not defined NODE_EXE call :probe "%ProgramFiles(x86)%"
if not defined NODE_EXE call :probe "%LOCALAPPDATA%"
if not defined NODE_EXE call :probe "%USERPROFILE%\AppData\Local\Programs"
if not defined NODE_EXE goto :prompt

:check_node
if not exist "%NODE_EXE%" (
  echo [SAC] WARNING: "%NODE_EXE%" does not exist.
  set "NODE_EXE="
  goto :prompt
)
"%NODE_EXE%" -e "process.exit(0)" >nul 2>nul
if errorlevel 1 (
  echo [SAC] WARNING: "%NODE_EXE%" does not run as a standalone node.
  set "NODE_EXE="
  goto :prompt
)
goto :run

:prompt
echo.
echo [SAC] A usable node.exe was not found automatically.
echo [SAC] DeepSeek Harness ships its own copy, usually at:
echo       %%LOCALAPPDATA%%\Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\node\bin\node.exe
echo [SAC] Tip: drag node.exe onto this window and press Enter.
echo.
set "NODE_EXE="
set /p NODE_EXE=Enter the full path to node.exe: 
if not defined NODE_EXE goto :no_node
set "NODE_EXE=%NODE_EXE:"=%"
goto :check_node

:run
if not exist "%~dp0update-sac.js" (
  echo [SAC] ERROR: update-sac.js was not found next to this script.
  echo [SAC] Keep update-sac.cmd and update-sac.js in the same folder.
  goto :no_node
)
echo [SAC] Using node: "%NODE_EXE%"
echo.
"%NODE_EXE%" "%~dp0update-sac.js" %*
exit /b %ERRORLEVEL%

:no_node
echo.
pause
exit /b 1

rem --- subroutine: look for node.exe under "DeepSeek Harness*\resources\runtime\..." ---
:probe
for /d %%D in ("%~1\DeepSeek Harness*") do (
  if not defined NODE_EXE if exist "%%~D\resources\runtime\primary-runtime\dependencies\node\bin\node.exe" set "NODE_EXE=%%~D\resources\runtime\primary-runtime\dependencies\node\bin\node.exe"
  if not defined NODE_EXE if exist "%%~D\resources\runtime\bin\node.exe" set "NODE_EXE=%%~D\resources\runtime\bin\node.exe"
)
goto /eof
