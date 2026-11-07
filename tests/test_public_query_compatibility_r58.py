"""Public rule/model compatibility for literal queries and generic status."""
import copy
import pytest
import canaan_intent as ci
import canaan_turn_pipeline as turns
from canaan_query_context import trusted_query_bindings


@pytest.fixture(autouse=True)
def empty_installed_application_index(monkeypatch):
    """These query contracts do not depend on applications installed on a host."""
    from canaan_os_executor import CanaanOSExecutor
    monkeypatch.setattr(CanaanOSExecutor,'scan_system_applications',classmethod(lambda cls,**kwargs:{}))
    monkeypatch.setattr(CanaanOSExecutor,'load_contact_aliases',classmethod(lambda cls:{'aliases':{}}),raising=False)

CASES = [('查一下C盘还剩多少空间', 'run_cmd', 'Get-PSDrive -Name C | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name C | Select-Object Used,Free', 'rule', 0, False), ('E盘还剩多少空间', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'rule', 0, False), ('查一下E盘还剩多少容量', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'rule', 0, False), ('看看磁盘剩余空间', 'run_cmd', 'Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,Size,FreeSpace', 'run_cmd', 'Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,Size,FreeSpace', 'rule', 0, False), ('开机多久了', 'run_cmd', '(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'run_cmd', '(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'rule', 0, False), ('查一下开机了多长时间', 'run_cmd', '(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'run_cmd', '(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'rule', 0, False), ('IP地址是多少', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'rule', 0, False), ('查一下本机IP地址', 'run_cmd', 'Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress', 'run_cmd', 'Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress', 'rule', 0, False), ('查一下本机网络配置', 'run_cmd', 'ipconfig /all', 'run_cmd', 'ipconfig /all', 'rule', 0, False), ('执行命令 Get-PSDrive -Name E | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'llm', 1, True), ('执行命令 Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,Size,FreeSpace', 'run_cmd', 'Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,Size,FreeSpace', 'run_cmd', 'Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,Size,FreeSpace', 'llm', 1, True), ('执行命令 (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'run_cmd', '(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'run_cmd', '(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime', 'llm', 1, True), ('执行命令 Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress', 'run_cmd', 'Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress', 'run_cmd', 'Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress', 'llm', 1, True), ('执行命令 ipconfig', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'llm', 1, True), ('执行命令 ipconfig /all', 'run_cmd', 'ipconfig /all', 'run_cmd', 'ipconfig /all', 'llm', 1, True), ('查询电脑状态', 'system_status', '', 'system_status', '', 'rule', 0, False), ('查下系统状态', 'system_status', '', 'system_status', '', 'rule', 0, False), ('查一下硬盘还剩多少容亮', 'system_status', '', 'system_status', '', 'rule', 0, False), ('看看CPU占用', 'system_status', '', 'system_status', '', 'rule', 0, False), ('运行 ipconfig', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'llm', 1, True), ('请运行 ipconfig', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'llm', 1, True), ('执行 PowerShell命令 ipconfig', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'llm', 1, True), ('执行命令“ipconfig”', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'llm', 1, True), ('请运行 "ipconfig /all"', 'run_cmd', 'ipconfig /all', 'run_cmd', 'ipconfig /all', 'llm', 1, True), ('执行命令 IPCONFIG', 'run_cmd', 'ipconfig', 'run_cmd', 'ipconfig', 'llm', 1, True), ('运行 ipconfig   /all', 'run_cmd', 'ipconfig /all', 'run_cmd', 'ipconfig /all', 'llm', 1, True), ('运行 Get-PSDrive   -Name E | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'run_cmd', 'Get-PSDrive -Name E | Select-Object Used,Free', 'llm', 1, True), ('我这台机器现在的运行负荷怎样', 'system_status', '', 'system_status', '', 'llm', 1, True), ('检查硬件当前负荷', 'system_status', '', 'system_status', '', 'llm', 1, True), ('给我报一下机器此刻的负荷', 'system_status', '', 'system_status', '', 'llm', 1, True), ('看下这台设备运行得怎么样', 'system_status', '', 'system_status', '', 'llm', 1, True), ('我有点担心，检查硬件当前负荷', 'system_status', '', 'system_status', '', 'llm', 1, True), ('这台机器今天感觉有些慢，给我报一下它此刻的负荷', 'system_status', '', 'system_status', '', 'llm', 1, True), ('我有点担心，CPU温度多少度', 'system_status', '', 'system_status', '', 'rule', 0, False), ('我有点担心，显卡温度高不高', 'system_status', '', 'system_status', '', 'llm', 1, True), ('CPU温度多少度，因为我有点担心', 'system_status', '', 'system_status', '', 'rule', 0, False), ('显卡温度高不高，因为我有点担心', 'system_status', '', 'system_status', '', 'llm', 1, True), ('检查硬件当前负荷，我有点担心', 'system_status', '', 'system_status', '', 'llm', 1, True)]

@pytest.mark.parametrize('text,action,param,expected_action,expected_param,old_source,old_calls,old_fast_none', CASES)
def test_original_public_query_route_and_binding_remain_compatible(text,action,param,expected_action,expected_param,old_source,old_calls,old_fast_none):
    proposal={'action':action,'param':param,'_conf':.99,'_pconf':.99}
    calls=[]
    def model(source):calls.append(source);return copy.deepcopy(proposal)
    fast=ci.fast_match_intent(text)
    if action=='system_status' and old_fast_none and text in (
        '我这台机器现在的运行负荷怎样','检查硬件当前负荷','给我报一下机器此刻的负荷',
        '看下这台设备运行得怎么样','我有点担心，检查硬件当前负荷',
        '这台机器今天感觉有些慢，给我报一下它此刻的负荷','检查硬件当前负荷，我有点担心'):
        assert fast is None
    state=turns.decide(text,None,None,[ci.fast_match_intent],model,lambda item:item,
        lambda source,item,is_llm:ci.apply_command_guard(source,item,require_confidence=is_llm))
    decision=state['decision']
    assert decision['action']==expected_action and decision.get('param','')==expected_param
    assert state['exec_user_text']==text and not state['confirmed']
    assert len(calls)<=old_calls and all(source==text for source in calls)
    if old_source=='rule':assert state['exec_source']=='rule' and calls==[]
    binding,=trusted_query_bindings(text,[decision])
    assert binding.original_text==text and binding.action==expected_action
    assert not any(name in binding.__dataclass_fields__ for name in ('confirmed','authorized','_conf'))
