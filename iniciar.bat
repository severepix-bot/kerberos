@echo off
cd /d "%~dp0"
if not defined CEREBRO_SERVER_URL set "CEREBRO_SERVER_URL=https://cerebrocentra.com"
where npm >nul 2>nul
if errorlevel 1 (
  echo Node.js/npm e necessario para instalar os plugins MCP. Instale em https://nodejs.org/
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -m venv .venv
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
if not exist .plugins\node_modules\@modelcontextprotocol\server-filesystem\dist\index.js call :install_mcp || exit /b 1
if not exist .plugins\node_modules\@modelcontextprotocol\server-memory\dist\index.js call :install_mcp || exit /b 1
if not exist .plugins\node_modules\next-devtools-mcp\dist\index.js call :install_mcp || exit /b 1
if not exist .plugins\node_modules\@modelcontextprotocol\server-github\dist\index.js call :install_mcp || exit /b 1
if not exist .plugins\node_modules\@googleworkspace\cli\run.js call :install_mcp || exit /b 1
if not exist .plugins\node_modules\@microsoft\workiq\bin\workiq.js call :install_mcp || exit /b 1
if not exist .plugins\node_modules\apple-photos-mcp\build\index.js call :install_mcp || exit /b 1
.venv\Scripts\python.exe -m playwright install chromium
.venv\Scripts\python.exe -m assistente %*
goto :eof

:install_mcp
npm install --prefix .plugins --no-save --package-lock=false @modelcontextprotocol/server-filesystem@2026.8.31 @modelcontextprotocol/server-memory@2026.8.31 next-devtools-mcp@0.4.0 @modelcontextprotocol/server-github@2025.4.8 @googleworkspace/cli@0.22.5 @microsoft/workiq@1.0.1 apple-photos-mcp@2.1.20
exit /b %errorlevel%
