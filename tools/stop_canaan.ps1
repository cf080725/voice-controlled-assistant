# 停止团团：托盘监管程序、hub、GPT-SoVITS 语音服务（只按命令行精确匹配，不动其他 python 程序和 Ollama）
$root = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $root "data"
New-Item -ItemType Directory -Force $dataDir | Out-Null
Set-Content -Path (Join-Path $dataDir "hub_stopped.flag") -Value "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 停止团团.bat" -Encoding UTF8

# 托盘可能是 canaan_tray.py 启动的（开机自启），也可能是 canaan.py tray（安装包的启动脚本）；灵动岛是 canaan.py island
$patterns = @('canaan_tray.py', 'canaan_desk_os_hub.py', 'canaan.py*tray', 'canaan.py*island', 'canaan_island.py')
$killed = 0
foreach ($p in $patterns) {
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like "*$p*" } | ForEach-Object {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}
    }
}
# GPT-SoVITS 语音服务（api_v2.py 且端口 9880）
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*api_v2.py*' -and $_.CommandLine -like '*9880*' } | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}
}
Start-Sleep -Seconds 2
$left = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and ($_.CommandLine -like '*canaan_tray.py*' -or $_.CommandLine -like '*canaan_desk_os_hub.py*' -or $_.CommandLine -like '*canaan.py*tray*' -or $_.CommandLine -like '*canaan.py*island*' -or ($_.CommandLine -like '*api_v2.py*' -and $_.CommandLine -like '*9880*')) }
if ($left) {
    Write-Host "[!] 还有进程没结束：" ($left | ForEach-Object ProcessId)
} else {
    Write-Host "[OK] 团团已全部停止（结束了 $killed 个进程）。下次开机登录会自动启动；想现在启动，双击“启动团团（托盘版）.bat”。"
}
