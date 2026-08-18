"""
CET4Prep 本地认证系统 — 配置
开发电脑即本地服务器：SQLite + 内存 TTL Store（验证码），未来可平滑替换 Redis。
"""
import os
import pathlib
import secrets

BASE_DIR = pathlib.Path(__file__).resolve().parent          # server/
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "vocab_auth.db"
SECRET_KEY_FILE = BASE_DIR / ".secret_key"


def _load_secret_key() -> str:
    """首次启动生成随机密钥并持久化；生产环境可用环境变量覆盖。"""
    if SECRET_KEY_FILE.exists():
        key = SECRET_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = secrets.token_urlsafe(48)
    SECRET_KEY_FILE.write_text(key, encoding="utf-8")
    return key


# JWT
SECRET_KEY = os.environ.get("VOCAB_SECRET_KEY") or _load_secret_key()
ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = int(os.environ.get("VOCAB_ACCESS_MINUTES", "60"))     # Access Token 有效期（v9.123：15→60 分钟，过期后由 Refresh 自动续期）
REFRESH_TOKEN_DAYS = int(os.environ.get("VOCAB_REFRESH_DAYS", "7"))          # Refresh Token 有效期（7 天，超过需重新登录）

# 图形验证码
CAPTCHA_TTL_SECONDS = 5 * 60        # 5 分钟有效
CAPTCHA_MAX_ATTEMPTS = 5            # 连续错 5 次作废

# 短信验证码（Mock Provider）
SMS_TTL_SECONDS = 5 * 60            # 5 分钟有效
SMS_COOLDOWN_SECONDS = 60           # 同一手机号 + 用途 60 秒冷却
SMS_MAX_ATTEMPTS = 5                # 连续错 5 次作废

# 消息撤回（v9.91）：发送后 2 分钟内可撤回
REVOKE_WINDOW_SECONDS = 120

# v9.93 管理员预置：环境变量可覆盖（VOCAB_ADMIN_USER / VOCAB_ADMIN_PASS / VOCAB_ADMIN_SECRET）；
# 默认随机生成并写入 server/.admin_credentials.txt（仅服务端持有，客户端不硬编码）
ADMIN_USERNAME = os.environ.get("VOCAB_ADMIN_USER", "Administer")
ADMIN_CRED_FILE = BASE_DIR / ".admin_credentials.txt"

# 开发模式：开放「获取最近一条短信验证码」调试接口（本地无真实短信供应商时的联调手段）
DEBUG = os.environ.get("VOCAB_DEBUG", "1") == "1"
