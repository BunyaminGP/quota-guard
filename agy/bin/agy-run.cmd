@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "CANDIDATE_ROOT=%%~fI"

if defined AGY_QUOTA_GUARD_HOME (
    set "PLUGIN_ROOT=%AGY_QUOTA_GUARD_HOME%"
) else if exist "%CANDIDATE_ROOT%\scripts\agy_runner.py" (
    set "PLUGIN_ROOT=%CANDIDATE_ROOT%"
) else (
    set "PLUGIN_ROOT=%USERPROFILE%\.agents\plugins\quota-guard\agy"
)

where python >nul 2>nul
if not errorlevel 1 goto :run_python

where python3 >nul 2>nul
if not errorlevel 1 goto :run_python3

echo [agy-quota-guard] Error: python3 or python is required. 1>&2
exit /b 127

:run_python
python "%PLUGIN_ROOT%\scripts\agy_runner.py" %*
exit /b %ERRORLEVEL%

:run_python3
python3 "%PLUGIN_ROOT%\scripts\agy_runner.py" %*
exit /b %ERRORLEVEL%
