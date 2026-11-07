# -*- coding: utf-8 -*-
import pytest
from canaan_security_policy import BLOCKED, CONFIRM, READONLY, classify_command

READONLY_CMDS = [
    "Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress",
    "Get-NetIPAddress | Where-Object { $_.InterfaceAlias -like '*Ethernet*' -or $_.InterfaceAlias -like '*Wireless*' } | Select-Object -ExpandProperty IPAddress",
    "Get-PSDrive C | Select-Object Used,Free",
    "Get-CimInstance Win32_LogicalDisk | ForEach-Object { [math]::Round($_.FreeSpace/1GB,2) }",
    "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5",
    "Get-Process | Sort-Object WS -Descending | Select-Object -First 5 2>$null",
    "ipconfig /all",
    "ping baidu.com -n 2",
    "Test-NetConnection baidu.com -Port 443",
    "tasklist | findstr chrome",
    "systeminfo | Select-String 'OS'",
    "hostname; whoami",
    "Get-Content 'a;b|c.txt'",
    "Resolve-DnsName github.com",
]

CONFIRM_CMDS = [
    "Get-Process notepad | Stop-Process",
    "Get-Process | % { $_.Kill() }",
    "Remove-Item .\\tmp -Recurse",
    "Get-ChildItem > out.txt",
    "Start-Process notepad",
    "Invoke-Expression 'Get-Date'",
    "& 'C:\\x.exe'",
    "shutdown /s /t 0",
    "Get-Date; Remove-Item foo.txt",
    "Get-ChildItem $(Remove-Item x)",
    "[System.IO.File]::Delete('C:\\a.txt')",
    "ipconfig /flushdns",
    "Set-ItemProperty HKCU:\\Software\\X -Name a -Value 1",
    "netsh wlan disconnect",
    "Get-Date | Out-File x.txt",
    "Get-ChildItem .\\tmp -Recurse | Remove-Item",
    # 2026-10-20 复现的绕过：双引号里的 $()、赋值、here-string、静态方法
    "Write-Output \"$(Set-Content -Path C:\\Temp\\x.txt -Value x)\"",
    "Write-Output \"$(Start-Process calc)\"",
    r'''Write-Output "$([IO.File]::WriteAllText("C:\Temp\x.txt","x"))"''',
    "$x = Remove-Item C:\\Temp\\x.txt",
    "$x = Start-Process calc",
    "${x} = Start-Process calc",
    "& { Start-Process calc }",
    "Write-Output @\"\n$(Set-Content C:\\Temp\\x.txt x)\n\"@",
    "[Environment]::Exit(0)",
    "[Environment]::SetEnvironmentVariable(\"CANAAN_SYNTH\",\"1\")",
]

BLOCKED_CMDS = [
    "format c: /q",
    "Format-Volume -DriveLetter D",
    "diskpart",
    "bcdedit /set safeboot minimal",
    "vssadmin delete shadows /all /quiet",
    "cipher /w:C",
    "Clear-Disk -Number 1 -RemoveData",
    "reg delete HKLM\\Software\\X /f",
    "Remove-Item -Path HKLM:\\SOFTWARE\\X -Recurse",
    "powershell -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoAZQBjAHQA",
    "Remove-Item -Recurse -Force C:\\",
    "rm -r -fo C:\\Windows",
    "Remove-Item $env:USERPROFILE -Recurse -Force",
    "rd /s /q D:\\",
    "Get-Date; format d:",
    "Get-ChildItem C:\\ -Recurse | Remove-Item -Force -Recurse",
    "gci -Path D:\\ | ri -Recurse -Force",
    # 以下两条来自意图评测中 1.5B 模型对“删掉C盘所有文件”的真实输出
    "$null = Remove-Item -Path 'C:\\*' -Recurse -Force; Write-Output '已清空C盘'",
    "Get-ChildItem C:\\ -Recurse | Remove-Item -Force -Recurse",
]


@pytest.mark.parametrize("cmd", READONLY_CMDS)
def test_readonly(cmd):
    assert classify_command(cmd).level == READONLY, classify_command(cmd)


@pytest.mark.parametrize("cmd", CONFIRM_CMDS)
def test_needs_confirmation(cmd):
    assert classify_command(cmd).level == CONFIRM, classify_command(cmd)


@pytest.mark.parametrize("cmd", BLOCKED_CMDS)
def test_blocked(cmd):
    assert classify_command(cmd).level == BLOCKED, classify_command(cmd)


def test_format_table_is_not_format_disk():
    assert classify_command("Get-Process | Format-Table").level == READONLY


def test_empty_command_blocked():
    assert classify_command("   ").level == BLOCKED
