"""
CET4Prep 本地认证系统 — 短信验证码（Mock Provider）

业务规则（全部真实实现，与真实供应商流程一致）：
- 验证码：secrets 随机 6 位数字（绝不写死万能码）
- TTL：5 分钟有效
- 发送冷却：同一手机号 + 同一用途 60 秒内不能重发
- 失败次数：连续错 5 次验证码作废
- 一次性：校验成功立即作废
- Provider 抽象：未来替换为真实 SMS 供应商（阿里云/Twilio）只需实现 SMSProvider

开发调试：DEBUG 模式下开放 GET /api/auth/debug/last-sms 读取最近一条（本地联调用）。
"""
import logging
import secrets
from abc import ABC, abstractmethod

import config
import stores

logger = logging.getLogger("sms")


class SMSProvider(ABC):
    @abstractmethod
    def send(self, phone: str, code: str, purpose: str) -> None: ...


class MockSMSProvider(SMSProvider):
    """本地模拟发送：真实供应商在此接入（Twilio / 阿里云短信 API）。"""

    def send(self, phone: str, code: str, purpose: str) -> None:
        logger.info("[MOCK-SMS] to=%s purpose=%s code=%s", phone, purpose, code)


sms_provider: SMSProvider = MockSMSProvider()


def send_sms_code(phone: str, purpose: str) -> tuple[bool, str]:
    """发送短信验证码。返回 (ok, message)。"""
    phone = phone.strip()
    if not _valid_phone(phone):
        return False, "手机号格式不正确"

    cooldown_key = f"sms_cooldown:{phone}:{purpose}"
    remain = stores.store.ttl(cooldown_key)
    if remain > 0:
        return False, f"发送过于频繁，请 {remain} 秒后再试"

    code = f"{secrets.randbelow(10**6):06d}"        # 随机 6 位数字
    code_id = stores.new_key()
    stores.store.set(f"sms:{code_id}", code, config.SMS_TTL_SECONDS)
    stores.store.set(f"sms_phone:{phone}:{purpose}", code_id, config.SMS_TTL_SECONDS)
    stores.store.set(cooldown_key, "1", config.SMS_COOLDOWN_SECONDS)
    sms_provider.send(phone, code, purpose)
    return True, "验证码已发送"


def verify_sms_code(phone: str, purpose: str, code: str) -> bool:
    """一次性校验：成功作废；连错 SMS_MAX_ATTEMPTS 次作废。"""
    key = f"sms_phone:{phone}:{purpose}"
    code_id = stores.store.get(key)
    if not code_id:
        return False
    ckey = f"sms:{code_id}"
    expect = stores.store.get(ckey)
    if expect is None:
        return False
    if (code or "").strip() != expect:
        if stores.store.incr_fail(ckey, config.SMS_MAX_ATTEMPTS) >= config.SMS_MAX_ATTEMPTS:
            stores.store.delete(ckey)
            stores.store.delete(key)
        return False
    stores.store.delete(ckey)
    stores.store.delete(key)
    return True


def last_sms_code(phone: str, purpose: str) -> str | None:
    """开发调试：读取最近一条未消费的验证码明文（仅 DEBUG 模式开放）。"""
    code_id = stores.store.get(f"sms_phone:{phone}:{purpose}")
    if not code_id:
        return None
    return stores.store.get(f"sms:{code_id}")


def _valid_phone(phone: str) -> bool:
    return len(phone) == 11 and phone.isdigit() and phone.startswith("1")
