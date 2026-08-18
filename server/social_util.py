"""
CET4Prep 社交模块 — 公共工具（好友状态 / 会话 / 通知 / 公开信息序列化）
被 friendship / message / file / notification 路由共享，保持平铺风格一致。
"""
import db


def pair(a: int, b: int) -> tuple[int, int]:
    """A-B 归一化（a<b），保证好友/会话唯一。"""
    return (a, b) if a < b else (b, a)


def get_friend_status(me_id: int, target_id: int) -> str:
    """好友状态：self / friends / pending_sent / pending_received / none"""
    if me_id == target_id:
        return "self"
    a, b = pair(me_id, target_id)
    if db.query_one("SELECT id FROM friendships WHERE user_a_id=? AND user_b_id=?", (a, b)):
        return "friends"
    if db.query_one(
            "SELECT id FROM friend_requests WHERE sender_id=? AND receiver_id=? AND status='pending'",
            (me_id, target_id)):
        return "pending_sent"
    if db.query_one(
            "SELECT id FROM friend_requests WHERE sender_id=? AND receiver_id=? AND status='pending'",
            (target_id, me_id)):
        return "pending_received"
    return "none"


def get_black_status(me_id: int, target_id: int) -> str:
    """拉黑状态（含方向）：none / blocked_by_me（我拉黑对方）/ blocked_me（对方拉黑我）。
    blacklists 表显式存 blocker/blocked 两列。"""
    if me_id == target_id:
        return "none"
    if db.query_one("SELECT id FROM blacklists WHERE blocker_id=? AND blocked_id=?", (me_id, target_id)):
        return "blocked_by_me"
    if db.query_one("SELECT id FROM blacklists WHERE blocker_id=? AND blocked_id=?", (target_id, me_id)):
        return "blocked_me"
    return "none"


def is_blacked(me_id: int, target_id: int) -> bool:
    """两人之间是否存在拉黑关系（任一方向）。存在则禁止互发申请/消息。"""
    if me_id == target_id:
        return False
    return db.query_one(
        "SELECT id FROM blacklists WHERE (blocker_id=? AND blocked_id=?) OR (blocker_id=? AND blocked_id=?)",
        (me_id, target_id, target_id, me_id)) is not None


def get_or_create_conversation(user_a: int, user_b: int) -> int:
    """取或建 1对1 会话（A<B 归一化）。返回 conversation_id。"""
    a, b = pair(user_a, user_b)
    row = db.query_one("SELECT id FROM conversations WHERE user_a_id=? AND user_b_id=?", (a, b))
    if row:
        return row["id"]
    cur = db.execute("INSERT INTO conversations (user_a_id, user_b_id) VALUES (?,?)", (a, b))
    return cur.lastrowid


def create_notification(user_id: int, ntype: str, title: str, content: str = "",
                        related_id=None, related_pid=None) -> int:
    """创建站内通知；related_pid=对方公开 ID（前端据此跳转聊天/申请页）。返回通知 id（供 WS 推送）。"""
    cur = db.execute(
        "INSERT INTO notifications (user_id, type, title, content, related_id, related_pid) "
        "VALUES (?,?,?,?,?,?)",
        (user_id, ntype, title, content, related_id, related_pid),
    )
    # v9.94：每个用户最多保留 99 条最新通知，超限自动移除最旧（最新永不丢）
    db.execute(
        "DELETE FROM notifications WHERE user_id=? AND id NOT IN "
        "(SELECT id FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 99)",
        (user_id, user_id),
    )
    return cur.lastrowid


def public_user_dict(row, me_id: int) -> dict:
    """公开信息序列化——绝不返回手机号/哈希/Token。
    v9.89：增加 gender / signature / black_status（公开资料，性别保密态由前端展示"保密"）。"""
    return {
        "id": row["public_id"],           # 公开纯数字 ID（搜索用）
        "nickname": row["username"],
        "avatar": row["avatar"] or "",
        "gender": row["gender"] if "gender" in row.keys() else "secret",
        "signature": row["signature"] if "signature" in row.keys() else "",
        "friend_status": get_friend_status(me_id, row["id"]),
        "black_status": get_black_status(me_id, row["id"]),
    }
