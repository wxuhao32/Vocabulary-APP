"""
CET4Prep 本地认证系统 — 安全层
密码：Argon2id（argon2-cffi，OWASP 推荐）
令牌：JWT（PyJWT）— Access Token(15min) + Refresh Token(7d，服务端影子表可撤销/旋转)
"""
import datetime
import hashlib
import uuid

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

import config

_ph = PasswordHasher()


# ---------- 密码 ----------
def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    try:
        _ph.verify(password_hash, plain)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def validate_password_strength(pwd: str) -> str | None:
    """密码强度校验（注册/重置/改密入口强制）：≥8 位 + 含字母 + 含数字。返回错误信息或 None。"""
    if not pwd or len(pwd) < 8:
        return "密码至少 8 位"
    if not any(c.isalpha() for c in pwd):
        return "密码需包含字母"
    if not any(c.isdigit() for c in pwd):
        return "密码需包含数字"
    return None


# ---------- JWT ----------
def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def create_access_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "type": "access",
        "exp": _now() + datetime.timedelta(minutes=config.ACCESS_TOKEN_MINUTES),
        "iat": _now(),
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=config.ALGORITHM)


def create_refresh_token(user_id: int) -> tuple[str, str, str]:
    """返回 (token, jti, expires_at_iso) —— token 给客户端，jti 用于影子表。"""
    jti = uuid.uuid4().hex
    exp = _now() + datetime.timedelta(days=config.REFRESH_TOKEN_DAYS)
    payload = {"sub": str(user_id), "type": "refresh", "jti": jti, "exp": exp, "iat": _now()}
    token = jwt.encode(payload, config.SECRET_KEY, algorithm=config.ALGORITHM)
    return token, jti, exp.isoformat()


def create_admin_step1_token(admin_id: int) -> str:
    """v9.93：管理员第一重认证通过的临时凭证（3 分钟，仅用于换取 admin_token）。"""
    payload = {
        "sub": str(admin_id), "type": "admin_step1",
        "exp": _now() + datetime.timedelta(minutes=3), "iat": _now(),
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=config.ALGORITHM)


def create_admin_token(admin_id: int) -> str:
    """v9.93：管理员后台访问 Token（2 小时）。独立类型，与普通用户 access 完全隔离。"""
    payload = {
        "sub": str(admin_id), "type": "admin",
        "exp": _now() + datetime.timedelta(hours=2), "iat": _now(),
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=config.ALGORITHM)


def decode_token(token: str, expected_type: str | None = None) -> dict:
    """解析并校验 JWT；无效/过期抛 jwt.PyJWTError。"""
    payload = jwt.decode(token, config.SECRET_KEY, algorithms=[config.ALGORITHM])
    if expected_type and payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"token type mismatch: {payload.get('type')}")
    return payload


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
