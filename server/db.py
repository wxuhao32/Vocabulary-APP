"""
CET4Prep 本地认证系统 — SQLite 数据层
表：
  users            用户账户（Argon2id 密码哈希）
  refresh_tokens   Refresh Token 影子表（存哈希，支持撤销/旋转）
  sms_codes        短信验证码影子表（存哈希 + 消费标记，双保险；主要 TTL 在内存 store）
"""
import sqlite3
import threading
import config

_WRITE_LOCK = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account       TEXT    NOT NULL UNIQUE,          -- 登录账号（用户名）
    username      TEXT    NOT NULL,                 -- 昵称
    phone         TEXT    NOT NULL UNIQUE,          -- 手机号（登录凭据之一）
    password_hash TEXT    NOT NULL,                 -- Argon2id
    avatar        TEXT    NOT NULL DEFAULT '',      -- 头像 dataURL
    gender        TEXT    NOT NULL DEFAULT 'secret',-- v9.89：性别 male / female / secret（保密）
    signature     TEXT    NOT NULL DEFAULT '',      -- v9.89：个性签名（公开资料）
    banned        INTEGER NOT NULL DEFAULT 0,       -- v9.93：封禁标记（1=已封禁）
    ban_reason    TEXT    NOT NULL DEFAULT '',      -- v9.93：封禁原因
    banned_at     TEXT,                             -- v9.93：封禁时间
    ban_until     TEXT,                             -- v9.95：解封时间（永久封禁为 NULL/空）
    ban_duration  TEXT    NOT NULL DEFAULT '',      -- v9.95：封禁时长描述（如 7天 / 永久 / 自定义）
    banned_by     TEXT    NOT NULL DEFAULT '',      -- v9.95：执行封禁的管理员用户名
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS refresh_tokens (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT    NOT NULL UNIQUE,             -- sha256(refresh_token)
    device_id  TEXT    NOT NULL DEFAULT '',
    expires_at TEXT    NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rt_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_rt_hash  ON refresh_tokens(token_hash);

CREATE TABLE IF NOT EXISTS sms_codes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    phone      TEXT    NOT NULL,
    purpose    TEXT    NOT NULL,                    -- register / reset / rebind
    code_hash  TEXT    NOT NULL,                    -- sha256(code) 双保险
    expires_at TEXT    NOT NULL,
    consumed   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_codes(phone, purpose);

-- ============ v9.87 社交模块（好友 / 私聊 / 文件 / 通知） ============
-- users 增列 public_id（纯数字唯一公开 ID，可搜索，不可修改）：
--   首次启动迁移：ALTER TABLE users ADD COLUMN public_id TEXT；存量行回填。

CREATE TABLE IF NOT EXISTS friendships (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_a_id, user_b_id)                   -- 约定 a < b，杜绝 A-B/B-A 重复
);
CREATE INDEX IF NOT EXISTS idx_friendship_a ON friendships(user_a_id);
CREATE INDEX IF NOT EXISTS idx_friendship_b ON friendships(user_b_id);

CREATE TABLE IF NOT EXISTS friend_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    receiver_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending/accepted/rejected/cancelled/expired
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_fr_sender   ON friend_requests(sender_id);
CREATE INDEX IF NOT EXISTS idx_fr_receiver ON friend_requests(receiver_id);
CREATE INDEX IF NOT EXISTS idx_fr_status   ON friend_requests(status);

CREATE TABLE IF NOT EXISTS conversations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    last_message_at TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_a_id, user_b_id)                   -- 约定 a < b，1对1 隐式会话
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    sender_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type            TEXT    NOT NULL DEFAULT 'text',   -- text / file
    content         TEXT    NOT NULL DEFAULT '',
    file_id         INTEGER,
    revoked         INTEGER NOT NULL DEFAULT 0,        -- v9.91：撤回标记（1=已撤回，内容清空）
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    read_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    conversation_id INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    filename      TEXT    NOT NULL,
    mime          TEXT    NOT NULL DEFAULT 'application/octet-stream',
    size          INTEGER NOT NULL DEFAULT 0,
    storage_path  TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type       TEXT    NOT NULL,                     -- friend_request / friend_request_accepted / new_message / file_message
    title      TEXT    NOT NULL,
    content    TEXT    NOT NULL DEFAULT '',
    related_id INTEGER,                              -- 关联 friend_request / message / file id
    related_pid TEXT,                                -- v9.87：对方公开 ID（通知→聊天/申请页跳转）
    is_read    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read);

-- ============ v9.89 黑名单 / 动态 ============
-- 黑名单：显式记录"谁拉黑谁"（blocker 拉黑 blocked），保留方向；
-- 交互限制采用双向禁止（任一方向拉黑即不可互发），但方向用于 UI 展示与单向校验。
CREATE TABLE IF NOT EXISTS blacklists (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (blocker_id, blocked_id)
);
CREATE INDEX IF NOT EXISTS idx_bl_blocker ON blacklists(blocker_id);
CREATE INDEX IF NOT EXISTS idx_bl_blocked ON blacklists(blocked_id);

CREATE TABLE IF NOT EXISTS moments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content    TEXT    NOT NULL,
    visibility TEXT    NOT NULL DEFAULT 'public',   -- public / friends / private
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mom_user ON moments(user_id);
CREATE INDEX IF NOT EXISTS idx_mom_vis  ON moments(visibility, id);

-- ============ v9.93 管理员系统（与 users 完全独立） ============
CREATE TABLE IF NOT EXISTS admins (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL UNIQUE,           -- 预置管理员用户名（Administer）
    pass_hash    TEXT    NOT NULL,                  -- 第一重凭证 Argon2id
    secret_hash  TEXT    NOT NULL,                  -- 第二重安全密钥 Argon2id（独立）
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- v9.93 应用版本发布（管理员后台上传 APK）
CREATE TABLE IF NOT EXISTS app_versions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version      TEXT    NOT NULL,
    note         TEXT    NOT NULL DEFAULT '',
    force_update INTEGER NOT NULL DEFAULT 0,
    size         INTEGER NOT NULL DEFAULT 0,
    storage_path TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- v9.109 学习统计（按日聚合，真实学习行为上报）
CREATE TABLE IF NOT EXISTS study_daily (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    date         TEXT    NOT NULL,                 -- YYYY-MM-DD（服务器本地日期）
    word_count   INTEGER NOT NULL DEFAULT 0,       -- 当日背词数（增量累加）
    duration_sec INTEGER NOT NULL DEFAULT 0,       -- 当日学习时长（秒，增量累加）
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_id, date)
);
CREATE INDEX IF NOT EXISTS idx_sd_user ON study_daily(user_id);

-- v9.109 登录历史（展示用；不含敏感 Token/IP）
CREATE TABLE IF NOT EXISTS login_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device     TEXT    NOT NULL DEFAULT '',        -- 设备名（前端 UA 摘要）
    result     TEXT    NOT NULL DEFAULT 'success', -- success / fail
    detail     TEXT    NOT NULL DEFAULT '',        -- 失败原因（仅展示"密码错误"等粗粒度）
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_lh_user ON login_history(user_id);

-- v9.109 个人主页背景（每用户一条当前背景）
CREATE TABLE IF NOT EXISTS profile_bgs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    bg         TEXT    NOT NULL,                   -- dataURL 背景图
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- v9.109 主页背景点赞（按"被赞用户+点赞者"唯一，防重复；换背景时清空）
CREATE TABLE IF NOT EXISTS bg_likes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 被赞用户
    liker_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 点赞者
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (target_id, liker_id)
);
CREATE INDEX IF NOT EXISTS idx_bl_target ON bg_likes(target_id);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    try:
        # v9.89 迁移先行：blacklists 旧结构（a<b 归一化，丢方向）→ 重建为 blocker/blocked
        # 必须在 executescript 之前 DROP，否则 _SCHEMA 里的 CREATE INDEX 会引用不存在的列
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='blacklists'").fetchone():
            bcols = [r[1] for r in conn.execute("PRAGMA table_info(blacklists)")]
            if "blocker_id" not in bcols:
                conn.execute("DROP TABLE blacklists")
                conn.commit()
        conn.executescript(_SCHEMA)
        # v9.87 迁移：users 表补 public_id 列（存量用户回填，纯数字唯一）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "public_id" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN public_id TEXT")
        rows = conn.execute("SELECT id FROM users WHERE public_id IS NULL OR public_id=''").fetchall()
        for (uid,) in rows:
            conn.execute("UPDATE users SET public_id=? WHERE id=?",
                         (generate_public_id(conn), uid))
        # v9.87 迁移：notifications 表补 related_pid 列（通知跳转用）
        ncols = [r[1] for r in conn.execute("PRAGMA table_info(notifications)")]
        if "related_pid" not in ncols:
            conn.execute("ALTER TABLE notifications ADD COLUMN related_pid TEXT")
        # v9.89 迁移：users 表补 gender / signature 列（个人资料）
        ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "gender" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN gender TEXT NOT NULL DEFAULT 'secret'")
        if "signature" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN signature TEXT NOT NULL DEFAULT ''")
        # v9.91 迁移：messages 表补 revoked 列（消息撤回）
        mcols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        if "revoked" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0")
        # v9.112 迁移：messages 表补 notified 列（接收方「系统通知栏已推送」状态，SQLite 持久化幂等防重复；
        #             与 read_at「已读」完全独立 —— 收到通知 ≠ 已读）
        if "notified" not in mcols:
            conn.execute("ALTER TABLE messages ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
        # v9.93 迁移：users 表补 banned / ban_reason / banned_at 列（封禁机制）
        ucols2 = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "banned" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        if "ban_reason" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT NOT NULL DEFAULT ''")
        if "banned_at" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN banned_at TEXT")
        # v9.95 迁移：users 表补 ban_until / ban_duration / banned_by 列（封禁时长与自动解封）
        if "ban_until" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN ban_until TEXT")
        if "ban_duration" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN ban_duration TEXT NOT NULL DEFAULT ''")
        if "banned_by" not in ucols2:
            conn.execute("ALTER TABLE users ADD COLUMN banned_by TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


def generate_public_id(conn=None) -> str:
    """生成 10 位纯数字唯一公开 ID（首位非 0，冲突重试）。"""
    import random
    _conn = conn or get_conn()
    try:
        while True:
            pid = str(random.randint(10**9, 10**10 - 1))
            if not _conn.execute("SELECT 1 FROM users WHERE public_id=?", (pid,)).fetchone():
                return pid
    finally:
        if conn is None:
            _conn.close()


# ============ v9.113 性能优化：线程内 SQLite 连接复用 ============
# 原实现每次 execute/query 都新建连接（PRAGMA foreign_keys/WAL + 连接建立），
# 实测单条发送链路 4 次 DB 操作共耗 ~300-500ms（连接开销为主）。
# 改为线程内缓存连接（thread-local）：单进程 FastAPI 线程池内连接复用，
# 写操作仍受 _WRITE_LOCK 串行保护；SQLite 单写者语义不变。
_local = threading.local()


def _cached_conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = get_conn()
        _local.conn = c
    return c


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """写操作（自动 commit）。SQLite 单写锁 + 短事务（线程内连接复用）。"""
    with _WRITE_LOCK:
        cur = _cached_conn().execute(sql, params)
        _cached_conn().commit()
        return cur


def query(sql: str, params: tuple = ()) -> list:
    return _cached_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return _cached_conn().execute(sql, params).fetchone()
