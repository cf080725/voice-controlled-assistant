# -*- coding: utf-8 -*-
"""
Canaan Desk OS - Commercial License & Hardware Authentication Engine
Provides hardware fingerprinting, machine-bound cryptographic license validation,
trial management, and admin key generation.
"""
import os
import sys
import json
import time
import hmac
import hashlib
import subprocess
from datetime import datetime
from typing import Dict, Any, Tuple, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
LICENSE_FILE = os.path.join(DATA_DIR, "canaan_license.key")

# 授权签名密钥：不写在源码里（源码会进公开版）。按顺序读：环境变量 CANAAN_LICENSE_SECRET → data/canaan_license_secret.bin（data/ 不进 git）。
# 都没有就是没配置：验证一律不通过，也不能签发新授权。
LICENSE_SECRET_FILE = os.path.join(DATA_DIR, "canaan_license_secret.bin")


def _load_secret():
    env = os.environ.get("CANAAN_LICENSE_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    try:
        with open(LICENSE_SECRET_FILE, "rb") as f:
            return f.read().strip() or None
    except OSError:
        return None


COMMERCIAL_HMAC_SECRET = _load_secret()


def get_machine_fingerprint() -> str:
    """
    获取 Windows 唯一物理硬件指纹（结合主板 UUID + 机器 GUID），生成抗篡改机器码
    """
    raw_ids = []
    
    # 1. 读取 Windows 注册表 MachineGuid
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        if guid:
            raw_ids.append(str(guid).strip().lower())
    except Exception:
        pass

    # 2. 读取主板 UUID via PowerShell CimInstance
    try:
        ps_cmd = 'powershell -NoProfile -Command "(Get-CimInstance Win32_ComputerSystemProduct).UUID"'
        out = subprocess.check_output(ps_cmd, shell=True, timeout=3).decode('utf-8', errors='ignore').strip()
        if out and len(out) > 8:
            raw_ids.append(out.lower())
    except Exception:
        pass

    # Fallback to username + computername
    if not raw_ids:
        raw_ids.append(os.environ.get("COMPUTERNAME", "UNKNOWN-PC"))
        raw_ids.append(os.environ.get("USERNAME", "DEFAULT-USER"))

    combined = "|".join(raw_ids).encode("utf-8")
    h = hashlib.sha256(combined).hexdigest().upper()
    # 格式化为美观的机器码：M-XXXX-XXXX-XXXX
    return f"M-{h[0:4]}-{h[4:8]}-{h[8:12]}-{h[12:16]}"


def generate_license_key(machine_id: str, tier: str = "PRO", days_valid: int = 365, customer: str = "USER") -> str:
    """
    【管理员/发卡端使用】根据机器码签发专属防伪激活码
    """
    if not COMMERCIAL_HMAC_SECRET:
        raise RuntimeError("没有配置授权签名密钥（CANAAN_LICENSE_SECRET 或 data/canaan_license_secret.bin），不能签发授权")
    expire_ts = int(time.time() + days_valid * 86400) if days_valid > 0 else 0
    payload = f"{machine_id}:{tier}:{expire_ts}:{customer}"
    sig = hmac.new(COMMERCIAL_HMAC_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest().upper()
    short_sig = sig[:8]
    # 激活码形态: CANAAN-TIER-EXPIRE-SIG
    key = f"CAN-{tier}-{expire_ts}-{short_sig}"
    return key


class LicenseManager:
    def __init__(self, license_path: str = LICENSE_FILE):
        self.license_path = license_path
        self.machine_id = get_machine_fingerprint()

    def verify(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        验证当前本地激活状态，返回 (是否有效, 说明文本, 授权元数据)
        """
        if not COMMERCIAL_HMAC_SECRET:
            # 没配签名密钥（比如公开版）：不做授权限制，也不签发试用码
            return True, "开源版：无需授权", {"tier": "OPEN"}
        if not os.path.exists(self.license_path):
            # 首次运行：自动给予 7 天试用期 (Auto Trial)
            trial_key = generate_license_key(self.machine_id, tier="TRIAL", days_valid=7, customer="TrialUser")
            self.save_license(trial_key)
            return True, "当前处于 7 天商业试用期，全功能正常运行", {"tier": "TRIAL", "days_left": 7}

        try:
            with open(self.license_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                data = json.loads(content) if content.startswith("{") else {"key": content}

            key_str = data.get("key", "").strip()
            parts = key_str.split("-")
            if len(parts) != 4 or parts[0] != "CAN":
                return False, "激活码格式无效", {}

            _, tier, expire_str, sig = parts
            expire_ts = int(expire_str)

            # 1. 检查是否过期
            if expire_ts > 0 and time.time() > expire_ts:
                return False, f"商业授权已到期（到期日: {datetime.fromtimestamp(expire_ts).strftime('%Y-%m-%d')}）", {"expired": True}

            # 2. 验签合法性与机器码绑定
            # 试用版/通用版与单机版校验
            payload = f"{self.machine_id}:{tier}:{expire_ts}:"
            # 校验包含用户名的验签
            expected_sig = None
            for cust in ["USER", "TrialUser", "PRO_USER", "ENTERPRISE", "FOUNDER"]:
                test_payload = f"{self.machine_id}:{tier}:{expire_ts}:{cust}"
                test_sig = hmac.new(COMMERCIAL_HMAC_SECRET, test_payload.encode("utf-8"), hashlib.sha256).hexdigest().upper()[:8]
                if test_sig == sig:
                    expected_sig = test_sig
                    break

            if not expected_sig:
                return False, "激活码与当前设备机器码不匹配，防盗版机制已拦截", {}

            days_left = int((expire_ts - time.time()) / 86400) if expire_ts > 0 else 9999
            return True, f"商业正版授权有效（版本: {tier}，剩余 {days_left} 天）", {
                "tier": tier,
                "days_left": days_left,
                "expire_date": datetime.fromtimestamp(expire_ts).strftime("%Y-%m-%d") if expire_ts > 0 else "永久授权"
            }

        except Exception as e:
            return False, f"激活码解析异常: {e}", {}

    def save_license(self, key_str: str) -> bool:
        os.makedirs(os.path.dirname(self.license_path), exist_ok=True)
        try:
            with open(self.license_path, "w", encoding="utf-8") as f:
                json.dump({
                    "key": key_str.strip(),
                    "machine_id": self.machine_id,
                    "updated_at": time.time()
                }, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False
