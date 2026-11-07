# 把团团进度提示插件装进本机 VS Code（不用打包、不用联网）：复制到 VS Code 的扩展目录，重启 VS Code 生效。
# 用法：powershell -ExecutionPolicy Bypass -File integrations\vscode-canaan\install.ps1
# 卸载：删除 %USERPROFILE%\.vscode\extensions\local.canaan-progress-0.1.0 目录。
$ErrorActionPreference = "Stop"
$src = $PSScriptRoot
$pkg = Get-Content (Join-Path $src "package.json") -Raw -Encoding utf8 | ConvertFrom-Json
$dst = Join-Path $env:USERPROFILE ".vscode\extensions\$($pkg.publisher).$($pkg.name)-$($pkg.version)"
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
New-Item -ItemType Directory -Force $dst | Out-Null
foreach ($f in "package.json", "extension.js", "lib.js", "README.md") { Copy-Item (Join-Path $src $f) $dst }
Write-Host "已安装到 $dst"
Write-Host "下一步：重启 VS Code；在团团看板「设置 → 外部客户端令牌」点「给 VS Code / 编程助手生成令牌」，"
Write-Host "        再在 VS Code 命令面板运行「团团：设置进度令牌」粘贴进去。"
