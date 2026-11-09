@echo off
setlocal
for %%I in ("%~dp0..\..\..\..") do set "ROOT=%%~fI"
set "TEMP=%ROOT%\office\.tmp_cli"
set "TMP=%TEMP%"
set "OFFICECLI_NO_AUTO_RESIDENT=1"
if not exist "%TEMP%" mkdir "%TEMP%" >nul 2>nul
"%ROOT%\office\tools\officecli\officecli.exe" %*
set "RC=%ERRORLEVEL%"
endlocal & exit /b %RC%
