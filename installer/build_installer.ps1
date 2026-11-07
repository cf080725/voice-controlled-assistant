# 团团安装包构建：嵌入式 Python + 依赖 + 程序文件 →（有 Inno Setup 时）编译 setup.exe，否则输出便携版 zip。
# 所有入口都走 canaan.py（目录拆分后脚本在 core/ shells/ 里）。模型不打进安装包：安装后首次运行 canaan.py bootstrap 下载（断点续传 + SHA-256 校验）。
# 不包含任何音色模型、游戏音频、个人数据（data/、logs/）。
#
# 用法: powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1 [-SkipDeps] [-Public] [-PyVersion 3.12.10]
param(
    [switch]$SkipDeps,
    [switch]$Public,
    [string]$PyVersion = "3.12.10",
    [string]$PipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$TkSource = ""     # 完整版 Python 的安装目录（同一个小版本）；不填就用 py -3.12 找到的那个
)
$ErrorActionPreference = "Stop"

# 嵌入式 Python 不带 tkinter，但悬浮岛兜底、桌宠、气泡都要用：从本机同版本的完整 Python 拷过来，
# 版本对不上就停止打包（_tkinter.pyd 和 python312.dll 不配套会直接崩），最后用嵌入式 Python 实际开一次 Tk 自检。
function Add-TkInter([string]$Dest, [string]$Version, [string]$Source) {
    if (-not $Source) { $Source = (& py -3.12 -c "import sys; print(sys.base_prefix)").Trim() }
    $srcVer = (& (Join-Path $Source "python.exe") -c "import platform; print(platform.python_version())").Trim()
    if ($srcVer -ne $Version) { throw "tkinter 来源 Python 是 $srcVer，嵌入式是 $Version，版本不一致，停止打包（用 -TkSource 指定同版本的 Python）" }
    Copy-Item (Join-Path $Source "Lib\tkinter") (Join-Path $Dest "tkinter") -Recurse -Force
    Remove-Item -Recurse -Force (Join-Path $Dest "tkinter\test") -ErrorAction SilentlyContinue
    Get-ChildItem (Join-Path $Dest "tkinter") -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
    foreach ($f in "_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "zlib1.dll") {
        $p = Join-Path $Source "DLLs\$f"
        if (Test-Path $p) { Copy-Item $p $Dest -Force } elseif ($f -ne "zlib1.dll") { throw "来源 Python 缺少 $f（安装时没勾选 tcl/tk）" }
    }
    Copy-Item (Join-Path $Source "tcl") (Join-Path $Dest "tcl") -Recurse -Force   # _tkinter 在 sys.prefix\tcl\tcl8.6 找 Tcl 库
    $check = & (Join-Path $Dest "python.exe") -c "import tkinter; r = tkinter.Tk(); r.withdraw(); print('tk', r.tk.call('info', 'patchlevel')); r.destroy()"
    if ($LASTEXITCODE -ne 0 -or "$check" -notmatch '^tk 8\.6') { throw "嵌入式 Python 的 tkinter 自检失败：$check" }
    Write-Host "      tkinter 自检通过（$check）"
}
$root = Split-Path -Parent $PSScriptRoot
$build = Join-Path $PSScriptRoot "build"
$app = Join-Path $build "app"
$py = Join-Path $app "python"
New-Item -ItemType Directory -Force $build | Out-Null
if (Test-Path $app) { Remove-Item -Recurse -Force $app }
New-Item -ItemType Directory -Force $app | Out-Null

# 1. 程序文件：git 跟踪的文件，去掉测试、归档、训练、评测、私人数据和未验收的报告
Push-Location $root
$excludePrefixes = @(
    'tests/', 'archive/', 'training_suite/', 'installer/', 'dataset_',
    'reports/', 'data/', 'logs/', 'docs/eval_', 'docs/research_',
    'desktop_pet_community_react/', 'desktop-pet-community-repo/'
)
$excludeFiles = @('总结.md', 'app_dump.js', 'catalog_dump.js')
if ($Public) {
    $excludePrefixes += @('message_tools/')
    $excludeFiles += @('personas/tuantuan.txt', 'resources/keywords_tuantuan.txt', 'tuantuan_tts_infer.yaml')
}
function Test-PackExcluded([string]$path) {
    foreach ($p in $excludePrefixes) {
        if ($p.EndsWith('/')) { if ($path.StartsWith($p)) { return $true } }
        elseif ($path -eq $p) { return $true }
    }
    if ($excludeFiles -contains $path) { return $true }
    if ($path -match '(^|/)__pycache__/') { return $true }
    if ($path -match '\.(pyc|pyo|wav|mp3|ogg|flac|m4a|ckpt|pth|zip|7z|rar|bak\d*)$') { return $true }
    return $false
}
$files = git -c core.quotepath=false ls-files | Where-Object {
    -not (Test-PackExcluded $_) -and $_ -notmatch '^(train_|merge_|generate_|combine_)'
}
foreach ($f in $files) {
    $dst = Join-Path $app $f
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    Copy-Item $f $dst
}
# 安装包里没有 .git，/health 的 build 字段从这里读
$commit = (git rev-parse HEAD).Trim()
[IO.File]::WriteAllText((Join-Path $app "build_info.json"), "{`"commit`": `"$commit`", `"built_at`": `"$(Get-Date -Format s)`"}", (New-Object Text.UTF8Encoding($false)))
# 构建清单：commit、是否公开、每个文件的 SHA-256，便于验收和比对
$manifest = Get-ChildItem $app -Recurse -File | ForEach-Object {
    [pscustomobject]@{ path = $_.FullName.Substring($app.Length + 1).Replace('\','/'); sha256 = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(); size = $_.Length }
}
[pscustomobject]@{ commit = $commit; built_at = (Get-Date -Format s); public = [bool]$Public; files = $manifest } |
    ConvertTo-Json -Depth 4 | Set-Content (Join-Path $app 'build_manifest.json') -Encoding UTF8
Pop-Location
if ($Public) {
    function Test-PublicLeak([string]$path) {
        foreach ($p in @('data/', 'logs/', 'reports/', 'docs/eval_', 'docs/research_', 'message_tools/')) {
            if ($path.StartsWith($p)) { return $true }
        }
        return @('tuantuan_tts_infer.yaml', 'personas/tuantuan.txt', 'resources/keywords_tuantuan.txt') -contains $path
    }
    $leaks = Get-ChildItem $app -Recurse -File | ForEach-Object { $_.FullName.Substring($app.Length + 1).Replace('\','/') } | Where-Object { Test-PublicLeak $_ }
    if ($leaks) { throw "公开构建包含禁止文件：$($leaks -join ', ')" }
}
Write-Host "[1/4] 程序文件 $($files.Count) 个（Public=$Public）"

# 2. 嵌入式 Python（国内镜像）
$zip = Join-Path $build "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    $urls = @("https://registry.npmmirror.com/-/binary/python/$PyVersion/python-$PyVersion-embed-amd64.zip",
              "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip")
    foreach ($u in $urls) { try { curl.exe -sSL -f -o $zip $u; if ($LASTEXITCODE -eq 0) { break } } catch {} }
}
Expand-Archive $zip -DestinationPath $py -Force
$pth = Get-ChildItem $py -Filter "python*._pth" | Select-Object -First 1
(Get-Content $pth.FullName) -replace '^#import site', 'import site' | Set-Content $pth.FullName -Encoding ascii
Add-Content $pth.FullName "..`n"   # 让嵌入式 Python 能 import 程序目录里的模块
Add-TkInter -Dest $py -Version $PyVersion -Source $TkSource
Write-Host "[2/4] 嵌入式 Python $PyVersion（含 tkinter）"

# 3. 依赖
if (-not $SkipDeps) {
    $getpip = Join-Path $build "get-pip.py"
    if (-not (Test-Path $getpip)) { curl.exe -sSL -o $getpip "https://bootstrap.pypa.io/get-pip.py" }
    & "$py\python.exe" $getpip -i $PipIndex --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "get-pip 失败（返回码 $LASTEXITCODE）" }
    & "$py\python.exe" -m pip install torch --index-url https://download.pytorch.org/whl/cpu --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "torch 安装失败（返回码 $LASTEXITCODE）" }
    $reqFile = Join-Path $app "requirements-lock.txt"
    if (-not (Test-Path $reqFile)) { $reqFile = Join-Path $app "requirements.txt" }
    & "$py\python.exe" -m pip install -r $reqFile -i $PipIndex --no-warn-script-location
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败（返回码 $LASTEXITCODE）：$reqFile" }
    & "$py\python.exe" -c "import tkinter, numpy, requests"
    if ($LASTEXITCODE -ne 0) { throw "依赖自检失败（tkinter/numpy/requests）" }
    Write-Host "[3/4] 依赖已安装（$([IO.Path]::GetFileName($reqFile))）"
} else {
    Write-Host "[3/4] 跳过依赖（-SkipDeps）"
}

# 4. 启动脚本 + Inno Setup 脚本
@"
@echo off
cd /d "%~dp0"
python\python.exe canaan.py bootstrap
start "" python\pythonw.exe canaan.py tray
"@ | Set-Content (Join-Path $app "启动团团.bat") -Encoding Default
@"
@echo off
cd /d "%~dp0"
python\python.exe canaan.py bootstrap
pause
"@ | Set-Content (Join-Path $app "下载模型.bat") -Encoding Default

$version = (Get-Date -Format "yyyy.MM.dd")
$iss = @"
[Setup]
AppName=团团 Desk
AppVersion=$version
DefaultDirName={localappdata}\CanaanDesk
DefaultGroupName=团团 Desk
PrivilegesRequired=lowest
OutputDir=$build
OutputBaseFilename=CanaanDesk-Setup-$version
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
[Languages]
Name: "chs"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
[Files]
Source: "$app\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
[Icons]
Name: "{group}\团团"; Filename: "{app}\python\pythonw.exe"; Parameters: "canaan.py tray"; WorkingDir: "{app}"
Name: "{group}\停止团团"; Filename: "{app}\scripts\停止团团.bat"; WorkingDir: "{app}"
Name: "{group}\下载模型"; Filename: "{app}\下载模型.bat"; WorkingDir: "{app}"
Name: "{group}\卸载团团"; Filename: "{uninstallexe}"
[Run]
Filename: "{app}\python\python.exe"; Parameters: "canaan.py bootstrap"; WorkingDir: "{app}"; Description: "下载模型（首次需要，约 3GB，可断点续传）"; Flags: postinstall
[UninstallRun]
Filename: "{app}\python\python.exe"; Parameters: "canaan.py tray --uninstall-autostart"; WorkingDir: "{app}"; RunOnceId: "noautostart"
"@
$issPath = Join-Path $build "canaan.iss"
$iss | Set-Content $issPath -Encoding UTF8
$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc $issPath
    Write-Host "[4/4] 安装包已生成：$build\CanaanDesk-Setup-$version.exe"
} else {
    $portable = Join-Path $build "CanaanDesk-Portable-$version.zip"
    if (Test-Path $portable) { Remove-Item $portable }
    Compress-Archive -Path "$app\*" -DestinationPath $portable
    Write-Host "[4/4] 未安装 Inno Setup 6（https://jrsoftware.org/isdl.php，免费），已生成便携版：$portable 与脚本 $issPath"
}
