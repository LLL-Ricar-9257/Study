# 兼容入口：真正的启动 / 停止 / 状态逻辑统一在 scripts/mcp_services.py。
# 端口分配、日志目录与可执行文件路径都由那份 Python 脚本决定，避免两处配置漂移。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts/start_mcp_services.ps1 start
#   powershell -ExecutionPolicy Bypass -File scripts/start_mcp_services.ps1 stop
#   powershell -ExecutionPolicy Bypass -File scripts/start_mcp_services.ps1 status
param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'mcp_services.py'
$venvPython = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }

& $python $script $Action
exit $LASTEXITCODE
