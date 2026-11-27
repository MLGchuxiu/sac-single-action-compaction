@echo off
rem ==========================================================================
rem diagnose-sac.cmd - read-only SAC deployment report (Windows, no PowerShell)
rem Keep this file next to diagnose-sac.js, then double-click it, or run:
rem   diagnose-sac.cmd [-profile NAME] [-dsh-root PATH] [-app-asar PATH]
rem Nothing is modified. Copy the whole window output back for analysis.
rem ==========================================================================
setlocal EnableExtensions
set "SAC_PAUSE=1"
set "NODE_EXE="

for /f "delims=" %%N in ('where node 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%N"
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
echo.
set "NODE_EXE="
set /p NODE_EXE=Enter the full path to node.exe: 
if not defined NODE_EXE goto :no_node
set "NODE_EXE=%NODE_EXE:"=%"
goto :check_node

:run
if not exist "%~dp0diagnose-sac.js" (
  echo [SAC] ERROR: diagnose-sac.js was not found next to this script.
  goto :no_node
)
echo ===== SAC deployment diagnostics =====
echo Host: %COMPUTERNAME%   User: %USERNAME%   Time: %DATE% %TIME%
echo Node: "%NODE_EXE%"
echo.
"%NODE_EXE%" "%~dp0diagnose-sac.js" %*
exit /b 0

:no_node
echo.
pause
exit /b 1

:probe
for /d %%D in ("%~1\DeepSeek Harness*") do (
  if not defined NODE_EXE if exist "%%~D\resources\runtime\primary-runtime\dependencies\node\bin\node.exe" set "NODE_EXE=%%~D\resources\runtime\primary-runtime\dependencies\node\bin\node.exe"
  if not defined NODE_EXE if exist "%%~D\resources\runtime\bin\node.exe" set "NODE_EXE=%%~D\resources\runtime\bin\node.exe"
)
goto /eof
