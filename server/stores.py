"""
CET4Prep 本地认证系统 — 一次性状态存储抽象

第一阶段：MemoryTTLStore（进程内存，带 TTL / 失败计数 / 冷却计数）。
未来替换 Redis 时，实现同一 KVStore 接口（RedisTTLStore）即可，
业务层（captcha / sms）零改动 —— 满足「验证码存储逻辑抽象成独立接口」的约束。
"""
import threading
import time
import uuid
from abc import ABC, abstractmethod
from typing import Optional


class KVStore(ABC):
    """键值 + TTL + 失败计数的存储接口（验证码 / 冷却 / 一次性令牌）。"""

    @abstractmethod
    def set(self, key: str, value: str, ttl_seconds: int) -> None: ...

    @abstractmethod
    def get(self, key: str) -> Optional[str]: ...

    @abstractmethod
    def ttl(self, key: str) -> int:
        """返回 key 剩余有效秒数（不存在/已过期返回 0）。"""

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def incr_fail(self, key: str, max_fails: int) -> int:
        """失败计数 +1；超过 max_fails 自动删除该 key。返回当前计数。"""


class MemoryTTLStore(KVStore):
    def __init__(self):
        self._data: dict[str, tuple[str, float]] = {}   # key -> (value, expire_ts)
        self._fails: dict[str, int] = {}                # key -> fail_count
        self._lock = threading.Lock()

    def _sweep(self):
        now = time.time()
        expired = [k for k, (_, exp) in self._data.items() if exp <= now]
        for k in expired:
            self._data.pop(k, None)
            self._fails.pop(k, None)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        with self._lock:
            self._sweep()
            self._data[key] = (value, time.time() + ttl_seconds)
            self._fails.pop(key, None)

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            self._sweep()
            item = self._data.get(key)
            return item[0] if item else None

    def ttl(self, key: str) -> int:
        with self._lock:
            self._sweep()
            item = self._data.get(key)
            if not item:
                return 0
            return max(0, int(item[1] - time.time()))

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)
            self._fails.pop(key, None)

    def incr_fail(self, key: str, max_fails: int) -> int:
        with self._lock:
            self._sweep()
            if key not in self._data:
                return 0
            count = self._fails.get(key, 0) + 1
            if count >= max_fails:
                self._data.pop(key, None)
                self._fails.pop(key, None)
            else:
                self._fails[key] = count
            return count


# 全局唯一实例（单进程部署）
store = MemoryTTLStore()


def new_key() -> str:
    return uuid.uuid4().hex
