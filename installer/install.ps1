# 一键安装（双击仓库根目录的 一键安装.bat）：Python 3.12 → 虚拟环境 .venv → 依赖 → 首次设置（名字、大模型方式、下载模型）
# → 需要时装 Ollama → 桌面快捷方式。可以反复运行：装过的会跳过，下载会续传。
#   powershell -ExecutionPolicy Bypass -File installer\install.ps1 [-PipIndex <镜像>] [-NoShortcut] [-SkipSetup]
# 不想被提问（脚本 / AI 代装）时把选择都用参数给出，例如：
#   ... install.ps1 -Name 小安 -Llm local -LocalModel tuned
#   ... install.ps1 -Name 小安 -Llm api -Provider deepseek        （密钥放环境变量 CANAAN_DEEPSEEK_API_KEY）
#   ... install.ps1 -Name 小安 -Llm rules
param(
    [string]$PipIndex = "",   # 不填就依次试清华、阿里云，最后官方源
    [string]$Name = "",       # 助手名字，2–4 个汉字
    [ValidateSet("", "local", "api", "rules")][string]$Llm = "",
    [ValidateSet("", "base", "tuned")][string]$LocalModel = "",
    [string]$Provider = "",   # -Llm api 时：deepseek / qwen / kimi / zhipu / siliconflow / openai / claude，或任意名字 + -BaseUrl
    [string]$Model = "",
    [string]$BaseUrl = "",
    [switch]$NoShortcut,
    [switch]$SkipSetup
)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    [!] $msg" -ForegroundColor Yellow }
function Fail($msg) {
    Write-Host "`n[安装没完成] $msg" -ForegroundColor Red
    Write-Host "修好后重新双击 一键安装.bat 即可，已装好的部分会跳过。"
    exit 1
}

function Find-Python312 {
    try {
        $p = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
    } catch {}
    foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe")) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Download($urls, $dest) {
    foreach ($u in $urls) {
        try {
            Write-Host "    下载 $u"
            Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $dest -TimeoutSec 600
            return $true
        } catch { Warn "下载失败：$($_.Exception.Message)" }
    }
    return $false
}

# ---------------------------------------------------------------- 1. Python 3.12
Step "检查 Python 3.12"
$py = Find-Python312
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    用 winget 安装 Python 3.12（只装给当前用户）"
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $py = Find-Python312
    }
    if (-not $py) {
        $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        $ok = Download @("https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe",
                         "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe") $exe
        if (-not $ok) { Fail "Python 3.12 下载失败，请手动从 https://www.python.org/downloads/ 安装 3.12 版" }
        Write-Host "    安装 Python 3.12（只装给当前用户，带 tkinter 和 py 启动器）"
        Start-Process -Wait -FilePath $exe -ArgumentList "/quiet InstallAllUsers=0 Include_launcher=1 Include_tcltk=1 Include_test=0 PrependPath=0"
        $py = Find-Python312
    }
    if (-not $py) { Fail "装完还是找不到 Python 3.12，请手动安装后重试" }
}
Ok $py
& $py -c "import tkinter" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "这个 Python 没有 tkinter（界面要用）。请用 python.org 的安装包重新安装 3.12，勾选 tcl/tk" }

# ---------------------------------------------------------------- 2. 虚拟环境
Step "准备虚拟环境 .venv（依赖只装在这个文件夹里，不影响你电脑上别的 Python）"
$vpy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $vpy)) {
    & $py -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境失败" }
}
Ok ".venv"

# ---------------------------------------------------------------- 3. 依赖
Step "安装依赖（第一次约 1–2 GB，主要是 torch）"
$indexes = @()
if ($PipIndex) { $indexes += $PipIndex }
$indexes += @("https://pypi.tuna.tsinghua.edu.cn/simple", "https://mirrors.aliyun.com/pypi/simple", "")
function Pip-Install($pipArgs) {
    foreach ($ix in $indexes) {
        $label = if ($ix) { $ix } else { "官方源 pypi.org" }
        Write-Host "    源：$label"
        $extra = if ($ix) { @("-i", $ix) } else { @() }
        & $vpy -m pip install @pipArgs @extra --disable-pip-version-check --retries 2 --timeout 30
        if ($LASTEXITCODE -eq 0) { return $true }
        Warn "这个源装不上，换下一个"
    }
    return $false
}
# 本体（锁定版本）+ 界面自动操作 / MCP / 知识库 / 加密 / 语音消息用到的包
if (-not (Pip-Install @("-r", "requirements-lock.txt", "-r", "requirements-integrations.txt"))) {
    Fail "依赖安装失败，见上面 pip 的报错（网络不稳时重新运行即可，已下载的会复用）"
}
& $vpy -c "import sherpa_onnx, sounddevice, pystray, tkinter, uiautomation, pysilk" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "依赖装完但导入失败，见上面的报错" }
Ok "依赖"

# ---------------------------------------------------------------- 4. 首次设置
function Install-Ollama {
    if (Get-Command ollama -ErrorAction SilentlyContinue) { return $true }
    Step "安装 Ollama（本机模型要用）"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Ollama.Ollama --silent --accept-package-agreements --accept-source-agreements | Out-Host
    } else {
        $exe = Join-Path $env:TEMP "OllamaSetup.exe"
        if (Download @("https://ollama.com/download/OllamaSetup.exe") $exe) {
            Start-Process -Wait -FilePath $exe -ArgumentList "/SILENT /SUPPRESSMSGBOXES /NORESTART"
        }
    }
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        Warn "Ollama 没装上：请从 https://ollama.com 手动安装，然后重新运行安装"
        return $false
    }
    for ($i = 0; $i -lt 30; $i++) {   # 等 Ollama 服务起来
        & ollama list *> $null
        if ($LASTEXITCODE -eq 0) { break }
        Start-Sleep -Seconds 2
    }
    return $true
}

if (-not $SkipSetup) {
    Step "首次设置：给助手起名字、选大模型方式、下载模型（可中断，重新运行会续传）"
    $bootArgs = @()
    if ($Name) { $bootArgs += @("--name", $Name) }
    if ($Llm) { $bootArgs += @("--llm", $Llm) }
    if ($LocalModel) { $bootArgs += @("--local-model", $LocalModel) }
    if ($Provider) { $bootArgs += @("--provider", $Provider) }
    if ($Model) { $bootArgs += @("--model", $Model) }
    if ($BaseUrl) { $bootArgs += @("--base-url", $BaseUrl) }
    if ($Llm -eq "local") { Install-Ollama | Out-Null }   # 已经知道要本机模型：先装 Ollama，一次走完
    & $vpy canaan.py bootstrap @bootArgs
    $bootExit = $LASTEXITCODE
    if ($bootExit -eq 2) { Fail "首次设置的参数不对，见上面的提示" }
    $mode = & $vpy -c "import json,os; p=os.path.join('data','llm_providers.json'); print(json.load(open(p,encoding='utf-8')).get('mode','') if os.path.exists(p) else '')"
    if ($mode -eq "local" -and -not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        # 交互时才知道选了本机模型：现在装 Ollama，再把模型补上
        if (Install-Ollama) {
            $more = @("--llm", "local")
            if ($LocalModel) { $more += @("--local-model", $LocalModel) }
            & $vpy canaan.py bootstrap @more
            $bootExit = $LASTEXITCODE
        }
    }
    if ($bootExit -ne 0) { Warn "首次设置还有没完成的项目（见上），重新运行安装会续传" }
}

# ---------------------------------------------------------------- 5. 快捷方式
if (-not $NoShortcut) {
    Step "创建桌面快捷方式"
    $name = (& $vpy -c "import canaan, canaan_config as c; print(c.ASSISTANT_NAME)" 2>$null | Select-Object -Last 1)
    if (-not $name) { $name = "语音助手" }
    $lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "$name.lnk"
    $shell = New-Object -ComObject WScript.Shell
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = Join-Path $Root ".venv\Scripts\pythonw.exe"
    $s.Arguments = "`"$(Join-Path $Root 'canaan.py')`""
    $s.WorkingDirectory = $Root
    $s.Description = "启动 $name（托盘版）"
    $s.Save()
    Ok $lnk
}

Write-Host "`n安装完成。双击桌面上的快捷方式，或者仓库里的 启动.bat 启动；右下角托盘出现图标就是在运行了。" -ForegroundColor Green
Write-Host "以后想改名字或换大模型方式：.venv\Scripts\python.exe canaan.py bootstrap --name 新名字 / --llm api"
