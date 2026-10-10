@echo off
setlocal
cd /d "%~dp0"

for /f "tokens=1,2,*" %%A in ('reg query "HKCU\Environment" /v FUND_MONITOR_IFIND_AUTH_TOKEN 2^>nul') do if /i "%%A"=="FUND_MONITOR_IFIND_AUTH_TOKEN" set "FUND_MONITOR_IFIND_AUTH_TOKEN=%%C"
for /f "tokens=1,2,*" %%A in ('reg query "HKCU\Environment" /v FUND_MONITOR_INVESTODAY_MCP_API_KEY 2^>nul') do if /i "%%A"=="FUND_MONITOR_INVESTODAY_MCP_API_KEY" set "FUND_MONITOR_INVESTODAY_MCP_API_KEY=%%C"

set "PY_HOME=%LOCALAPPDATA%\Python\pythoncore-3.14-64"

if exist "%PY_HOME%\pythonw.exe" (
  start "" "%PY_HOME%\pythonw.exe" "%~dp0app.py" --desktop
) else (
  "%PY_HOME%\python.exe" "%~dp0app.py" --desktop
)

endlocal
